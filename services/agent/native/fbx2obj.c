/*
 * fbx2obj — FBX → OBJ（几何 + UV，烘焙世界变换）
 *
 * AssetAgent 的 FBX 导入转换器：基于 ufbx（单文件 C 解析器，MIT，
 * https://github.com/ufbx/ufbx）。之所以存在这个工具：
 *   - trimesh 读不了 FBX，纯 Python 方案不可靠；
 *   - 完整 Blender（300MB+）只为导入一个网格太重，改为可选拔回落；
 *   - 本工具编译后几百 KB，随应用分发，导入不再依赖任何外部软件。
 *
 * 设计约定（与 services/agent/app/tools/convert.py 配套）：
 *   - 输出 OBJ 供 trimesh 加载再转 GLB 工作副本（预览格式统一 GLB）；
 *   - v / vt 全量写出（不按面展开），f 行分别引用位置索引与 UV 索引，
 *     OBJ 的 `f v/vt` 允许两套索引并存，文件不膨胀；
 *   - 用 node->geometry_to_world 把顶点烘到世界坐标（对齐 bpy 路径的
 *     transform_apply 行为），行列式 < 0 时翻转绕序；
 *   - 无 UV 的网格输出 `f v v v` 形式。
 *
 * 编译（见 native/build.py）：
 *   gcc -O2 -o ufbx2obj.exe fbx2obj.c ufbx/ufbx.c -lm
 *
 * 用法：ufbx2obj <in.fbx> <out.obj>
 * 退出码：0 成功；1 解析/写盘失败；2 参数错误。错误详情写 stderr。
 */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "ufbx.h"

static double matrix_determinant(const ufbx_matrix *m) {
    // 3x3 左上块（cols[0..2]）的行列式，用于判断是否镜像变换
    double a = (double)m->cols[0].x, b = (double)m->cols[1].x, c = (double)m->cols[2].x;
    double d = (double)m->cols[0].y, e = (double)m->cols[1].y, f = (double)m->cols[2].y;
    double g = (double)m->cols[0].z, h = (double)m->cols[1].z, i = (double)m->cols[2].z;
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: ufbx2obj <in.fbx> <out.obj>\n");
        return 2;
    }
    const char *in_path = argv[1];
    const char *out_path = argv[2];

    ufbx_error error;
    ufbx_scene *scene = ufbx_load_file(in_path, NULL, &error);
    if (!scene) {
        char buf[1024];
        ufbx_format_error(buf, sizeof(buf), &error);
        fprintf(stderr, "ufbx parse failed: %s\n", buf);
        return 1;
    }

    FILE *out = fopen(out_path, "wb");
    if (!out) {
        fprintf(stderr, "cannot open output: %s\n", out_path);
        ufbx_free_scene(scene);
        return 1;
    }

    size_t global_v = 0;  // 已写的位置顶点数（OBJ 索引 1 起）
    size_t global_vt = 0; // 已写的 UV 顶点数
    size_t mesh_count = 0;

    // 材质引用：trimesh 只有在材质带贴图时才做 UV 拆分，
    // 配套的 .mtl / 占位白图由 Python 侧（tools/convert.py）写入同名文件
    {
        char mtl_name[512];
        const char *base = out_path, *slash;
        for (slash = out_path; *slash; slash++)
            if (*slash == '/' || *slash == '\\') base = slash + 1;
        if ((size_t)(slash - base) < sizeof(mtl_name) - 5) {
            snprintf(mtl_name, sizeof(mtl_name), "%.*s.mtl", (int)(slash - base), base);
            fprintf(out, "mtllib %s\nusemtl assetagent_dummy\n", mtl_name);
        }
    }

    for (size_t ni = 0; ni < scene->nodes.count; ni++) {
        ufbx_node *node = scene->nodes.data[ni];
        if (!node->mesh) continue; // 只取网格节点
        ufbx_mesh *mesh = node->mesh;
        if (mesh->vertex_position.values.count == 0) continue;

        const ufbx_matrix m = node->geometry_to_world;
        const double det = matrix_determinant(&m);
        const int mirrored = det < 0.0;

        // ---- 位置：全量写出，烘掉节点变换 ----
        for (size_t i = 0; i < mesh->vertex_position.values.count; i++) {
            ufbx_vec3 p = mesh->vertex_position.values.data[i];
            p = ufbx_transform_position(&m, p);
            fprintf(out, "v %.6f %.6f %.6f\n", (double)p.x, (double)p.y, (double)p.z);
        }

        // ---- UV：有就全量写出 ----
        const int has_uv = mesh->vertex_uv.values.count > 0;
        for (size_t i = 0; has_uv && i < mesh->vertex_uv.values.count; i++) {
            ufbx_vec2 uv = mesh->vertex_uv.values.data[i];
            fprintf(out, "vt %.6f %.6f\n", (double)uv.x, (double)uv.y);
        }

        // ---- 面：ufbx 三角化后按角索引引用 v / vt ----
        for (size_t fi = 0; fi < mesh->faces.count; fi++) {
            ufbx_face face = mesh->faces.data[fi];
            if (face.num_indices < 3) continue; // 点 / 线段不是面

            uint32_t indices_buf[3 * 64];
            uint32_t *indices = indices_buf;
            if (face.num_indices > 64) {
                indices = malloc(sizeof(uint32_t) * face.num_indices * 3);
                if (!indices) continue;
            }
            // 返回值是**三角形个数**（不是索引数），0 表示该面无法三角化
            uint32_t ntri = ufbx_triangulate_face(indices, face.num_indices * 3, mesh, face);
            if (ntri == 0) {
                if (face.num_indices > 64) free(indices);
                continue;
            }

            for (uint32_t t = 0; t < ntri; t++) {
                uint32_t c0 = indices[t * 3 + 0];
                uint32_t c1 = indices[t * 3 + (mirrored ? 2 : 1)];
                uint32_t c2 = indices[t * 3 + (mirrored ? 1 : 2)];

                uint32_t p0 = mesh->vertex_position.indices.data[c0] + 1 + global_v;
                uint32_t p1 = mesh->vertex_position.indices.data[c1] + 1 + global_v;
                uint32_t p2 = mesh->vertex_position.indices.data[c2] + 1 + global_v;

                if (has_uv) {
                    uint32_t t0 = mesh->vertex_uv.indices.data[c0] + 1 + global_vt;
                    uint32_t t1 = mesh->vertex_uv.indices.data[c1] + 1 + global_vt;
                    uint32_t t2 = mesh->vertex_uv.indices.data[c2] + 1 + global_vt;
                    fprintf(out, "f %u/%u %u/%u %u/%u\n", p0, t0, p1, t1, p2, t2);
                } else {
                    fprintf(out, "f %u %u %u\n", p0, p1, p2);
                }
            }
            if (face.num_indices > 64) free(indices);
        }

        global_v += mesh->vertex_position.values.count;
        if (has_uv) global_vt += mesh->vertex_uv.values.count;
        mesh_count++;
    }

    fclose(out);
    ufbx_free_scene(scene);

    if (mesh_count == 0 || global_v == 0) {
        fprintf(stderr, "no mesh geometry found in %s\n", in_path);
        remove(out_path);
        return 1;
    }

    fprintf(stderr, "ok: %zu mesh(es), %zu vertices -> %s\n", mesh_count, global_v, out_path);
    return 0;
}

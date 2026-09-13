"""多材质/贴图转移（方案 A）与装配体网格处理测试。

覆盖 2026-09-13 摩托复盘修出来的三个坑：
1. UV 缝复制顶点把连通网格拆成"UV 岛组件"（component_count / repair 误判）；
2. 全局减面在装配体上炸成碎片（逐组件路径）；
3. 导入模型贴图丢失（软件版底色烘焙 + 材质挂回 GLB）。
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

from app.tools import bake as bake_tool
from app.tools.decimate import decimate_mesh
from app.tools.mesh_io import component_count, load_mesh, save_mesh, welded_face_components
from app.tools.repair import repair_mesh
from app.tools.uv import unwrap


def _glb_images(path: Path) -> list:
    """直接解析 GLB 的 JSON chunk，返回 images 数组。"""
    data = Path(path).read_bytes()
    offset = 12
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack("<II", data[offset : offset + 8])
        if chunk_type == 0x4E4F534A:  # JSON
            import json

            return json.loads(data[offset + 8 : offset + 8 + chunk_len]).get("images", [])
        offset += 8 + chunk_len + (chunk_len % 4 and (4 - chunk_len % 4) or 0)
    return []


# ------------------------------------------------------------------ 组件焊接


def test_welded_components_ignores_uv_seams():
    """同一位置的两份顶点（glTF UV 缝的形态）必须焊成一个组件。"""
    box = trimesh.creation.box()
    # 复制一份顶点，一半面引第一份、一半面引第二份：位置完全重合、索引独立
    vertices = np.vstack([box.vertices, box.vertices])
    faces = np.vstack([box.faces, box.faces + len(box.vertices)])
    seamed = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    # 未焊接的邻接会看到 2 个"组件"，焊接后必须还是 1
    assert len(welded_face_components(seamed)) == 1
    assert component_count(seamed) == 1


def test_mesh_stats_components_use_welding():
    """mesh_stats 的组件数走同一套焊接（不报 UV 岛数）。"""
    box = trimesh.creation.box()
    vertices = np.vstack([box.vertices, box.vertices])
    faces = np.vstack([box.faces, box.faces + len(box.vertices)])
    seamed = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    seamed.visual = trimesh.visual.texture.TextureVisuals(uv=np.random.rand(len(vertices), 2))

    from app.tools.mesh_io import mesh_stats

    assert mesh_stats(seamed)["components"] == 1


# ------------------------------------------------------------------ repair 剔除判据


def test_repair_keeps_small_parts_near_body(tmp_path):
    """又小又少面的组件：贴着主体的保留（摩托的螺丝），飘远的剔除（生成模型的灰尘）。"""
    main = trimesh.creation.icosphere(subdivisions=3, radius=0.5)  # 642 面
    near = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    near.apply_translation([0.3, 0.3, 0.3])  # 主体包围盒内
    far = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    far.apply_translation([3.0, 3.0, 3.0])  # 主体外 5 米
    mesh = trimesh.util.concatenate([main, near, far])

    repaired, report = repair_mesh(mesh)

    actions = "; ".join(report["actions"])
    assert "剔除游离组件 1 个" in actions
    # 近处小零件还在：包围盒仍覆盖 (0.3, 0.3, 0.3) 附近
    low, high = repaired.bounds
    assert low[0] < 0.35 and high[0] > 0.25
    # 远处的没了
    assert high[0] < 1.0


# ------------------------------------------------------------------ 装配体减面


def test_decimate_assembly_keeps_all_parts():
    """60 个散布小球的装配体：逐组件减面要保住每一个组件的位置。"""
    parts = []
    centers = [(float(i % 10), float(i // 10), 0.0) for i in range(60)]
    for cx, cy, _ in centers:
        sphere = trimesh.creation.icosphere(subdivisions=2, radius=0.3)  # 320 面
        sphere.apply_translation([cx * 2.0, cy * 2.0, 0.0])
        parts.append(sphere)
    mesh = trimesh.util.concatenate(parts)
    target = 60 * 20  # 每球摊到 20 面

    decimated, report = decimate_mesh(mesh, target)

    assert report["method"].startswith("fast-simplification")
    assert decimated.faces.shape[0] <= target * 3  # 收敛允许少量超出
    # 每个球心附近都要有顶点（组件没有被炸飞）
    vertices = np.asarray(decimated.vertices)
    for cx, cy, _ in centers:
        distance = np.linalg.norm(vertices[:, :2] - np.array([cx * 2.0, cy * 2.0]), axis=1)
        assert distance.min() < 0.5, f"组件 ({cx}, {cy}) 在减面后消失了"


# ------------------------------------------------------------------ 底色烘焙（方案 A）


def _textured_box_glb(path: Path) -> None:
    """造一个带红/绿底色贴图的 GLB（走真实 trimesh 导出链路）。"""
    box = trimesh.creation.box()
    image = Image.new("RGB", (64, 64), (200, 30, 30))
    image.paste((30, 200, 30), (32, 0, 64, 64))  # 左红右绿
    # 顶点 UV 交错覆盖贴图左右两半，红绿都能被采到
    uv = np.tile(np.array([[0.25, 0.5], [0.75, 0.5]]), (len(box.vertices) // 2 + 1, 1))[
        : len(box.vertices)
    ]
    box.visual = trimesh.visual.texture.TextureVisuals(uv=uv, image=image)
    path.write_bytes(box.export(file_type="glb"))


def test_software_diffuse_bake_produces_colored_atlas(tmp_path):
    """无 Blender 兜底：软件投影烘焙产出带源模型颜色的底色图集。"""
    high_path = tmp_path / "high.glb"
    _textured_box_glb(high_path)

    high = load_mesh(high_path)
    low, report = unwrap(high)
    assert report["method"] == "xatlas"
    low_path = tmp_path / "low.glb"
    save_mesh(low, low_path)

    out_dir = tmp_path / "textures"
    result = bake_tool.bake_diffuse_software(low_path, high_path, out_dir, resolution=128)

    assert result["status"] == "ok", result.get("reason")
    atlas_path = Path(result["files"][0])
    assert atlas_path.exists()
    pixels = np.asarray(Image.open(atlas_path).convert("RGB")).reshape(-1, 3)
    red = np.sum((pixels[:, 0] > 120) & (pixels[:, 1] < 90))
    green = np.sum((pixels[:, 1] > 120) & (pixels[:, 0] < 90))
    assert red > 0 and green > 0, "图集应同时包含源贴图的红与绿"


def test_attach_baked_material_survives_glb_roundtrip(tmp_path):
    """挂上烘焙材质后存 GLB：文件里必须真的有贴图（视口/导出靠它）。"""
    import trimesh

    high_path = tmp_path / "high.glb"
    _textured_box_glb(high_path)
    high = load_mesh(high_path)
    low, _ = unwrap(high)
    low_path = tmp_path / "low.glb"
    save_mesh(low, low_path)

    texture_dir = tmp_path / "textures"
    texture_dir.mkdir()
    basecolor = texture_dir / "T_basecolor.png"
    Image.new("RGB", (32, 32), (180, 60, 60)).save(basecolor)

    mesh = load_mesh(low_path)
    textured = bake_tool.attach_baked_material(mesh, [basecolor])
    material = getattr(textured.visual, "material", None)
    assert getattr(material, "baseColorTexture", None) is not None

    out = tmp_path / "baked.glb"
    save_mesh(textured, out)
    assert _glb_images(out), "烘焙后的 GLB 应内嵌贴图"

    # 浏览器视口读的就是这个文件：用 trimesh 自带加载器（含材质）验证
    reloaded = trimesh.load(out, force="mesh", process=False)
    reloaded_material = getattr(reloaded.visual, "material", None)
    assert getattr(reloaded_material, "baseColorTexture", None) is not None


def test_glb_load_reports_source_material_scale(tmp_path):
    """加载器要报告源文件的材质/贴图规模（详情页展示"源 N 材质"）。"""
    high_path = tmp_path / "high.glb"
    _textured_box_glb(high_path)
    mesh = load_mesh(high_path)
    assert mesh.metadata["source_material_count"] >= 1
    assert mesh.metadata["source_image_count"] >= 1


# ------------------------------------------------------------------ 烘焙空间对齐（摩托复盘）


def _bake_and_measure(low_path: Path, high_path: Path, tmp: Path, **align) -> np.ndarray:
    result = bake_tool.bake_diffuse_software(low_path, high_path, tmp, resolution=128, **align)
    assert result["status"] == "ok", result.get("reason")
    return np.asarray(Image.open(result["files"][0]).convert("RGB"))


def _gradient_sphere_glb(path: Path) -> None:
    """球面经度渐变贴图的球：每个经度颜色唯一，无退化 UV 面。

    错位投影会把所有低模面都投到高模朝向观察方向的同一小块表面
    （同一经度 → 同色），与基准（各面各经度各色）形成可测差异。
    盒子不适合做这件事：法线沿 x/y 的面在 (x,y) 投影 UV 下退化成线，
    画不进图集；共享 UV 角点的盒子错位还能撞对颜色。
    """
    import trimesh

    sphere = trimesh.creation.icosphere(subdivisions=3, radius=0.5)
    vertices = np.asarray(sphere.vertices)
    u = (np.arctan2(vertices[:, 2], vertices[:, 0]) / (2 * np.pi)) + 0.5
    v = (vertices[:, 1] - vertices[:, 1].min()) / float(np.ptp(vertices[:, 1]))
    gradient = np.zeros((64, 64, 3), dtype=np.uint8)
    gradient[..., 0] = np.linspace(10, 245, 64)[None, :]  # R 沿经度变化
    gradient[..., 1] = 90
    gradient[..., 2] = np.linspace(200, 40, 64)[None, :]
    image = Image.fromarray(gradient)
    sphere.visual = trimesh.visual.texture.TextureVisuals(
        uv=np.stack([u, v], axis=1), image=image
    )
    path.write_bytes(sphere.export(file_type="glb"))


def test_software_bake_aligns_high_into_low_space(tmp_path):
    """低模被归一化过（缩放 + 挪轴心）、高模保持原始大小：烘焙必须把高模对齐进同一空间。

    摩托复盘：不对齐时最近点投影全打在错误位置，整车颜色串成深灰。
    注意高模的所有导出副本必须用 trimesh.load 起手（保留材质）——
    我们的 load_mesh 会丢材质，再导出就变成 2×2 占位图（HANDOFF 老坑），
    烘焙就没东西可采了。
    """
    from app.tools.repair import normalize_transform

    high_path = tmp_path / "high.glb"
    _gradient_sphere_glb(high_path)
    high = load_mesh(high_path)  # 几何用途（低模来源）；材质在这里已经丢了

    # 低模走管线同款流程：unwrap → normalize 到 expected_size 空间
    low, _ = unwrap(high)
    low, _ = normalize_transform(low, pivot="bottom_center", expected_size_m=1.0)
    low_path = tmp_path / "low.glb"
    save_mesh(low, low_path)

    # 高模的一切**带材质的导出**都从 trimesh.load 起手（材质完整）
    high_textured = trimesh.load(high_path, force="mesh", process=False)

    # 基准：高模手动 normalize 到同一空间，直接投影
    high_aligned, _ = normalize_transform(high_textured, pivot="bottom_center", expected_size_m=1.0)
    aligned_path = tmp_path / "high_aligned.glb"
    aligned_path.write_bytes(high_aligned.export(file_type="glb"))
    baseline = _bake_and_measure(low_path, aligned_path, tmp_path / "t0")
    # 基准本身必须是彩色的（渐变被采到），否则后面的对比没有意义
    assert np.abs(baseline.astype(int) - 127).mean() > 8, "基准图集不应是空灰底"

    # "原始文件"：放大 10 倍 + 平移，烘焙内部要对齐回低模空间
    big = high_textured.copy()
    big.apply_scale(10.0)
    big.apply_translation([40.0, -7.0, 3.0])
    big_path = tmp_path / "big.glb"
    big_path.write_bytes(big.export(file_type="glb"))

    aligned = _bake_and_measure(
        low_path, big_path, tmp_path / "t1", pivot="bottom_center", expected_size_m=1.0
    )
    diff = np.abs(aligned.astype(int) - baseline.astype(int))
    assert diff.mean() < 12, f"对齐烘焙应与基准一致，平均色差 {diff.mean():.1f}"

    # 反证：完全不对齐（origin 模式 = 不挪不缩），投影必然偏离基准
    misaligned = _bake_and_measure(low_path, big_path, tmp_path / "t2", pivot="origin")
    diff_bad = np.abs(misaligned.astype(int) - baseline.astype(int))
    assert diff_bad.mean() > diff.mean() + 10, "未对齐的高模应显著偏离基准"


def _two_uv_gltf_with_texcoord1(path: Path) -> None:
    """手写一个最小 .gltf：两套 UV，材质声明 baseColorTexture 用 TEXCOORD_1。

    TEXCOORD_0 全 0（若被误用则只会采到贴图单一像素），TEXCOORD_1 覆盖
    贴图红/绿两半。trimesh 只支持一套 UV，导不出这种文件，所以手工拼 JSON。
    """
    import base64
    import io
    import json

    box = trimesh.creation.box()
    positions = np.asarray(box.vertices, dtype="<f4")
    indices = np.asarray(box.faces, dtype="<u2")
    uv0 = np.zeros((len(positions), 2), dtype="<f4")
    uv1 = np.tile(
        np.array([[0.25, 0.5], [0.75, 0.5]], dtype="<f4"),
        (len(positions) // 2 + 1, 1),
    )[: len(positions)]

    image = Image.new("RGB", (64, 64), (200, 30, 30))
    image.paste((30, 200, 30), (32, 0, 64, 64))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    image_uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

    binary = bytearray()
    views: list[dict] = []
    accessors: list[dict] = []

    def push(arr: np.ndarray, component_type: int, type_: str) -> int:
        raw = arr.tobytes()
        views.append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(raw)})
        binary.extend(raw)
        accessors.append(
            {
                "bufferView": len(views) - 1,
                "componentType": component_type,
                "count": len(arr) if arr.ndim == 1 else len(arr),
                "type": type_,
            }
        )
        return len(accessors) - 1

    pos_acc = push(positions, 5126, "VEC3")
    idx_acc = push(indices.reshape(-1), 5123, "SCALAR")
    uv0_acc = push(uv0, 5126, "VEC2")
    uv1_acc = push(uv1, 5126, "VEC2")

    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [
            {
                "uri": "data:application/octet-stream;base64,"
                + base64.b64encode(bytes(binary)).decode(),
                "byteLength": len(binary),
            }
        ],
        "bufferViews": views,
        "accessors": accessors,
        "materials": [
            {"pbrMetallicRoughness": {"baseColorTexture": {"index": 0, "texCoord": 1}}}
        ],
        "textures": [{"source": 0}],
        "images": [{"uri": image_uri, "mimeType": "image/png"}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": pos_acc,
                            "TEXCOORD_0": uv0_acc,
                            "TEXCOORD_1": uv1_acc,
                        },
                        "indices": idx_acc,
                        "material": 0,
                        "mode": 4,
                    }
                ]
            }
        ],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    path.write_text(json.dumps(gltf), encoding="utf-8")


def test_software_bake_reads_material_texcoord_set(tmp_path):
    """材质 texCoord=N 时采样必须用第 N 套 UV（Sketchfab 常配 3~5 套）。

    摩托复盘：baseColor 贴图声明在非 0 套 UV 上，读 0 套会采到错误贴图
    区域（绿车变黑车）。"""
    high_path = tmp_path / "twouv.gltf"
    _two_uv_gltf_with_texcoord1(high_path)

    from app.tools.gltf_load import load_glb

    high, table = load_glb(high_path, with_materials=True)
    assert table.face_texture[0] is not None, "材质用 texCoord=1，应读到贴图"
    # 角点 UV 应来自 TEXCOORD_1（覆盖 0.25~0.75），而不是 TEXCOORD_0（全 0）
    assert table.face_uvs[:, :, 0].max() > 0.5, "应采到 TEXCOORD_1 的右半（>0.5）"

    low, _ = unwrap(high)
    low_path = tmp_path / "low.glb"
    save_mesh(low, low_path)
    atlas = _bake_and_measure(low_path, high_path, tmp_path / "t")
    flat = atlas.reshape(-1, 3).astype(int)
    green = np.sum((flat[:, 1] > flat[:, 0] + 30) & (flat[:, 1] > flat[:, 2] + 30))
    assert green > 50, f"texCoord=1 的红绿贴图应被采到（绿像素 {green}）"

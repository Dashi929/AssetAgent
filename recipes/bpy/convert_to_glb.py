"""把 FBX 等引擎原生格式转换成 GLB（工作副本）。

由 sidecar 通过 `blender --background --python` 调用，服务 FBX 导入（工作流 C）。
约定：成功打印 `OK {json}`，失败打印 `ERR {原因}`（与其它 bpy 脚本一致，
tools/blender.py 靠这两个标记判断结果）。

为什么导入时转、而不是管线每步现转：
Blender 冷启动要几秒，管线 7 步都要读网格。导入时转一次成 GLB，
后续所有步骤走 trimesh，又快又稳。原始 FBX 在 source/ 只读保存，
转出的 GLB 是版本节点的工作副本 —— 符合「源文件永不覆盖」约定。

用法：
    blender -b --factory-startup --python convert_to_glb.py -- \
        --input in.fbx --output out.glb
"""

from __future__ import annotations

import json
import os
import sys
import traceback

import bpy


def parse_args() -> dict[str, str]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args: dict[str, str] = {}
    for index in range(0, len(argv) - 1, 2):
        args[argv[index].lstrip("-")] = argv[index + 1]
    return args


def import_mesh(path: str) -> list:
    extension = os.path.splitext(path)[1].lower()
    if extension in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif extension == ".obj":
        # Blender 4.x 改名 wm.obj_import，3.x 及以下是 import_scene.obj
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif extension == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        raise RuntimeError(f"不支持的网格格式：{extension}")
    return [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]


def main() -> int:
    args = parse_args()
    src = args.get("input", "")
    dst = args.get("output", "")

    if not src or not os.path.exists(src):
        print(f"ERR 输入网格不存在：{src}")
        return 1
    if not dst:
        print("ERR 缺少 --output 参数")
        return 1

    # 清空默认场景（--factory-startup 会带一个默认立方体），避免混进导出
    bpy.ops.wm.read_factory_settings(use_empty=True)

    try:
        meshes = import_mesh(src)
    except Exception as exc:
        print(f"ERR 导入 {src} 失败：{exc}")
        return 1
    if not meshes:
        print("ERR 输入文件里没有网格对象")
        return 1

    # 应用旋转/缩放到世界坐标（FBX 常带节点变换），GLB 拿到的是干净几何。
    # 位置不动：轴心信息留给管线的轴心归一化步骤处理。
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    # 多对象合并成一个网格：GLB 单网格假设贯穿整条管线（trimesh load 会拼接，
    # 但拼接丢材质分组；先 join 一次更可控）
    if len(meshes) > 1:
        bpy.ops.object.join()

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    try:
        bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB")
    except TypeError:
        # Blender 版本间导出参数有增删，去掉可选参数重试
        bpy.ops.export_scene.gltf(filepath=dst)

    if not os.path.exists(dst):
        print("ERR Blender 未产出 GLB 文件")
        return 1

    print(
        "OK "
        + json.dumps(
            {
                "file": dst,
                "source": src,
                "objects": [obj.name for obj in bpy.context.scene.objects if obj.type == "MESH"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("ERR " + traceback.format_exc(limit=3))
        sys.exit(1)

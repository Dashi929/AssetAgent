"""按引擎预设导出 FBX。

由 sidecar 通过 `blender --background --python` 调用。
约定：成功打印 `OK {json}`，失败打印 `ERR {原因}`。

用法：
    blender -b --factory-startup --python export_asset.py -- \
        --mesh in.glb --out out.fbx --up_axis Z --unit_scale 100
"""

from __future__ import annotations

import json
import math
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
    mesh_path = args.get("mesh", "")
    out_path = args.get("out", "./export.fbx")
    up_axis = args.get("up_axis", "Y").upper()
    unit_scale = float(args.get("unit_scale", "1.0"))

    if not mesh_path or not os.path.exists(mesh_path):
        print(f"ERR 输入网格不存在：{mesh_path}")
        return 1

    bpy.ops.wm.read_factory_settings(use_empty=True)
    meshes = import_mesh(mesh_path)
    if not meshes:
        print("ERR 输入文件里没有网格对象")
        return 1
    meshes = list(bpy.context.selected_objects)

    # glTF 是 Y-up；UE 要 Z-up。绕 X 轴 -90° 把 Y-up 转到 Z-up。
    if up_axis == "Z":
        for obj in meshes:
            obj.rotation_mode = "XYZ"
            obj.rotation_euler.rotate_axis("X", -math.pi / 2)
        bpy.context.view_layer.update()
        bpy.ops.object.select_all(action="DESELECT")
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    export_kwargs = {
        "filepath": out_path,
        "use_selection": True,
        "global_scale": unit_scale,
        "apply_unit_scale": True,
        "apply_scale_options": "FBX_SCALE_NONE",
        "mesh_smooth_type": "FACE",
        "use_mesh_modifiers": True,
        "bake_anim": False,
        "path_mode": "COPY",
        "embed_textures": False,
    }

    try:
        bpy.ops.export_scene.fbx(**export_kwargs)
    except TypeError:
        # Blender 版本之间 FBX 导出参数有增删，去掉可选参数重试一次
        bpy.ops.export_scene.fbx(filepath=out_path, use_selection=True)

    if not os.path.exists(out_path):
        print("ERR Blender 未产出 FBX 文件")
        return 1

    print(
        "OK "
        + json.dumps(
            {
                "file": out_path,
                "up_axis": up_axis,
                "unit_scale": unit_scale,
                "objects": [obj.name for obj in meshes],
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("ERR " + traceback.format_exc(limit=3).replace("\n", " | "))
        sys.exit(1)

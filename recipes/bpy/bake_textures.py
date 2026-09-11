"""把高模细节烘焙到低模（normal / AO）。

由 sidecar 通过 `blender --background --python` 调用，参数在 `--` 之后。
约定：成功时打印 `OK {json}`，失败打印 `ERR {原因}`，sidecar 靠这个判断结果。

用法：
    blender -b --factory-startup --python bake_textures.py -- \
        --low low.glb --high high.glb --out ./textures --resolution 2048 --maps normal,ao
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
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif extension == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        raise RuntimeError(f"不支持的网格格式：{extension}")
    return [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]


def clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def new_bake_image(name: str, resolution: int, non_color: bool) -> bpy.types.Image:
    image = bpy.data.images.new(name, width=resolution, height=resolution, alpha=False)
    if non_color:
        # 法线/AO 是数据贴图，必须关掉色彩管理，否则颜色会被 sRGB 曲线扭曲
        image.colorspace_settings.name = "Non-Color"
    return image


def ensure_target_node(obj, image: bpy.types.Image) -> None:
    """给物体挂一个指向目标贴图的 Image Texture 节点，并设为激活节点。"""
    material = obj.data.materials[0] if obj.data.materials else None
    if material is None:
        material = bpy.data.materials.new(name=f"{obj.name}_bake")
        material.use_nodes = True
        obj.data.materials.append(material)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    node = next((n for n in nodes if n.type == "TEX_IMAGE"), None)
    if node is None:
        node = nodes.new("ShaderNodeTexImage")
    node.image = image
    nodes.active = node
    for other in nodes:
        other.select = other is node


def bake_map(
    low_objects: list,
    high_objects: list,
    image: bpy.types.Image,
    bake_type: str,
    samples: int,
) -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples

    bake = scene.render.bake
    bake.use_selected_to_active = bool(high_objects)
    bake.cage_extrusion = 0.05
    bake.margin = 16
    bake.use_clear = True

    bpy.ops.object.select_all(action="DESELECT")
    for obj in low_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = low_objects[0]
    for obj in high_objects:
        obj.select_set(True)

    for obj in low_objects:
        ensure_target_node(obj, image)

    bpy.ops.object.bake(type=bake_type, use_clear=True)


def save_image(image: bpy.types.Image, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    return path


def main() -> int:
    args = parse_args()
    low_path = args.get("low", "")
    high_path = args.get("high", "")
    out_dir = args.get("out", "./textures")
    resolution = int(args.get("resolution", "2048"))
    maps = [m.strip().lower() for m in args.get("maps", "normal,ao").split(",") if m.strip()]

    if not low_path or not os.path.exists(low_path):
        print(f"ERR 低模文件不存在：{low_path}")
        return 1

    clear_scene()

    low_objects = import_mesh(low_path)
    if not low_objects:
        print("ERR 低模里没有网格对象")
        return 1
    low_objects = list(bpy.context.selected_objects)

    high_objects = []
    if high_path and os.path.exists(high_path):
        high_objects = import_mesh(high_path)
        high_objects = list(bpy.context.selected_objects)
        # 高模只作为烘焙源，渲染时不需要它出现
        for obj in high_objects:
            obj.hide_render = True

    produced: list[str] = []
    for bake_name in maps:
        if bake_name == "normal":
            image = new_bake_image("bake_normal", resolution, non_color=True)
            bake_map(low_objects, high_objects, image, "NORMAL", samples=4)
            produced.append(save_image(image, os.path.join(out_dir, "T_normal.png")))
        elif bake_name == "ao":
            image = new_bake_image("bake_ao", resolution, non_color=True)
            bake_map(low_objects, high_objects, image, "AO", samples=16)
            produced.append(save_image(image, os.path.join(out_dir, "T_ao.png")))
        elif bake_name == "curvature":
            # curvature 在 Cycles 里没有直接 bake type，用 AO 反相近似，够美术判断转折
            image = new_bake_image("bake_curvature", resolution, non_color=True)
            bake_map(low_objects, high_objects, image, "AO", samples=16)
            produced.append(save_image(image, os.path.join(out_dir, "T_curvature.png")))
        else:
            print(f"ERR 不支持的烘焙类型：{bake_name}")
            return 1

    print("OK " + json.dumps({"files": produced, "resolution": resolution, "maps": maps}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("ERR " + traceback.format_exc(limit=3).replace("\n", " | "))
        sys.exit(1)

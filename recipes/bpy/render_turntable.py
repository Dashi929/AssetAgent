"""渲染转台图（供人工挑选与 Critic 视觉通道使用）。

由 sidecar 通过 `blender --background --python` 调用。
约定：成功打印 `OK {json}`，失败打印 `ERR {原因}`。

用法：
    blender -b --factory-startup --python render_turntable.py -- \
        --mesh in.glb --out ./turntable --frames 8 --size 512
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


def setup_environment(size: int) -> None:
    scene = bpy.context.scene
    # EEVEE 在无 GPU 的机器上也能跑，比 Cycles 快一个数量级，转台图够用
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = size
    scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"

    # 中性灰背景，和资产库的浅色主题一致
    world = bpy.data.worlds.new("turntable_world")
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    if background:
        background.inputs[0].default_value = (0.86, 0.85, 0.83, 1.0)
        background.inputs[1].default_value = 1.0


def add_lights() -> None:
    key = bpy.data.lights.new("key", type="AREA")
    key.energy = 800
    key.size = 4
    key_object = bpy.data.objects.new("key_light", key)
    key_object.location = (4, -4, 5)
    key_object.rotation_euler = (math.radians(45), 0, math.radians(45))
    bpy.context.collection.objects.link(key_object)

    fill = bpy.data.lights.new("fill", type="AREA")
    fill.energy = 250
    fill.size = 6
    fill_object = bpy.data.objects.new("fill_light", fill)
    fill_object.location = (-5, -3, 2)
    fill_object.rotation_euler = (math.radians(70), 0, math.radians(-60))
    bpy.context.collection.objects.link(fill_object)


def frame_camera(meshes: list, distance_factor: float = 2.6):
    """把相机架到能完整框住物体的位置。返回 (相机, 注视点)。"""
    import mathutils

    low = mathutils.Vector((1e9, 1e9, 1e9))
    high = mathutils.Vector((-1e9, -1e9, -1e9))
    for obj in meshes:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ mathutils.Vector(corner)
            low = mathutils.Vector((min(low[i], world_corner[i]) for i in range(3)))
            high = mathutils.Vector((max(high[i], world_corner[i]) for i in range(3)))
    center = (low + high) / 2
    radius = max((high - low).length / 2, 1e-3)

    camera_data = bpy.data.cameras.new("turntable_cam")
    camera_data.lens = 50
    camera = bpy.data.objects.new("turntable_cam", camera_data)
    bpy.context.collection.objects.link(camera)

    distance = radius * distance_factor
    camera.location = (center.x + distance * 0.75, center.y - distance * 0.75, center.z + distance * 0.45)
    direction = center - mathutils.Vector(camera.location)
    camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    bpy.context.scene.camera = camera
    return camera, center


def main() -> int:
    args = parse_args()
    mesh_path = args.get("mesh", "")
    out_dir = args.get("out", "./turntable")
    frames = max(1, int(args.get("frames", "8")))
    size = max(64, int(args.get("size", "512")))

    if not mesh_path or not os.path.exists(mesh_path):
        print(f"ERR 输入网格不存在：{mesh_path}")
        return 1

    bpy.ops.wm.read_factory_settings(use_empty=True)
    meshes = import_mesh(mesh_path)
    if not meshes:
        print("ERR 输入文件里没有网格对象")
        return 1
    meshes = list(bpy.context.selected_objects)

    setup_environment(size)
    add_lights()
    camera, center = frame_camera(meshes)

    import mathutils

    base_location = mathutils.Vector(camera.location)

    os.makedirs(out_dir, exist_ok=True)

    produced: list[str] = []
    for index in range(frames):
        angle = 2 * math.pi * index / frames
        # 相机绕 Z 轴公转（俯角不变），等价于物体自己在转台上一圈
        camera.location = mathutils.Matrix.Rotation(angle, 4, "Z") @ base_location
        direction = center - mathutils.Vector(camera.location)
        camera.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

        filepath = os.path.join(out_dir, f"turntable_{index:02d}.png")
        bpy.context.scene.render.filepath = filepath
        bpy.ops.render.render(write_still=True)
        produced.append(filepath)

    print("OK " + json.dumps({"files": produced, "frames": frames, "size": size}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("ERR " + traceback.format_exc(limit=3).replace("\n", " | "))
        sys.exit(1)

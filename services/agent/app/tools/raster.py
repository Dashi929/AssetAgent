"""纯 Python 软件渲染器 —— 在没有 Blender 的机器上出转台图与缩略图。

为什么需要它：MVP 的硬要求是"资产库有缩略图、变体挑选有转台图"，
但 Blender 是可选依赖（包体 300MB，不是每个美术的机器上都装了）。
用一个 z 排序 + Lambert 着色的软渲染顶上，画质比不上 bpy 渲染，
但"够用来判断形状对不对"，而且零依赖、跑得快。

Blender 存在时，render.py 会优先走 bpy。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw

# 环境光下限：给 0.25 是为了在法线不一致的网格上也不会出现大片死黑
AMBIENT = 0.25
LIGHT_DIR = np.array([-0.45, -0.75, 0.5], dtype=np.float64)
LIGHT_DIR /= np.linalg.norm(LIGHT_DIR)


def _prepare(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """居中并归一化到单位尺寸，保证任何资产渲染出来构图一致。"""
    prepared = mesh.copy()
    low, high = prepared.bounds
    prepared.apply_translation(-((low + high) / 2.0))
    extent = float(np.max(high - low))
    if extent > 1e-9:
        prepared.apply_scale(1.0 / extent)
    return prepared


def _shade(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """逐面 Lambert 着色系数，用绝对值避免法线不一致导致的黑块。"""
    tri = vertices[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths < 1e-12] = 1.0
    normals = normals / lengths
    lambert = np.abs(normals @ LIGHT_DIR)
    return AMBIENT + (1.0 - AMBIENT) * lambert


def _face_base_colors(mesh: trimesh.Trimesh, faces: np.ndarray) -> np.ndarray | None:
    """逐面取底色：材质带 baseColorTexture 且有 UV 时，按面中心 UV 采样贴图。

    每面采一个点（面中心重心 UV）对 256~512px 的缩略图/转台足够；
    引擎级正确性交给 Blender 转台路径，这里只要"缩略图有颜色"。
    """
    material = getattr(mesh.visual, "material", None)
    texture = getattr(material, "baseColorTexture", None)
    uv = getattr(mesh.visual, "uv", None)
    if texture is None or uv is None or not hasattr(texture, "size"):
        return None
    try:
        width, height = texture.size
        pixels = np.asarray(texture.convert("RGB"), dtype=np.float64) / 255.0
        face_uv = np.asarray(uv, dtype=np.float64)[faces].mean(axis=1)  # (F,2) 面中心
        u = np.mod(face_uv[:, 0], 1.0)
        v = np.mod(face_uv[:, 1], 1.0)
        # 图像行自上而下，UV 的 V 自下而上
        x = np.clip((u * width).astype(np.int64), 0, width - 1)
        y = np.clip(((1.0 - v) * height).astype(np.int64), 0, height - 1)
        return pixels[y, x]
    except Exception:
        return None


def _render_frame(
    mesh: trimesh.Trimesh,
    angle: float,
    size: int,
    background: tuple[int, int, int] = (247, 246, 243),
    base_color: tuple[int, int, int] = (170, 168, 160),
) -> Image.Image:
    rotation = trimesh.transformations.rotation_matrix(angle, [0.0, 1.0, 0.0])
    vertices = trimesh.transformations.transform_points(
        np.asarray(mesh.vertices, dtype=np.float64), rotation
    )
    faces = np.asarray(mesh.faces, dtype=np.int64)
    shades = _shade(vertices, faces)
    # 材质带底色贴图就按贴图走，没有才用灰模底色
    texture_colors = _face_base_colors(mesh, faces)

    # 正交投影：X→屏幕横轴，Y→屏幕纵轴（翻转使 +Y 朝上），Z→深度。
    # up 轴必须是 Y：管线内部网格统一 Y-up（GLB / three.js 视口同款约定），
    # 之前用 Z 当上轴，GLB 导入的模型在缩略图/转台里全是侧躺的。
    margin = 0.08
    scale = size * (1.0 - 2 * margin) / 2.0
    center = size / 2.0
    screen_x = center + vertices[:, 0] * scale
    screen_y = center - vertices[:, 1] * scale
    depth = vertices[:, 2]

    order = np.argsort(depth[faces].mean(axis=1))[::-1]  # 远的先画

    image = Image.new("RGB", (size, size), background)
    draw = ImageDraw.Draw(image)
    for face_index in order:
        face = faces[face_index]
        # 贴图色是 0~1 浮点、兜底灰是 0~255 整数 —— 统一成 0~1 再乘光照系数。
        # 之前直接 int(浮点*shade) 全被截成 0，带贴图的模型在软渲染里整台纯黑。
        if texture_colors is not None:
            base = texture_colors[face_index]
        else:
            base = np.asarray(base_color, dtype=np.float64) / 255.0
        color = tuple(
            int(min(255, channel * shades[face_index] * 255)) for channel in base
        )
        draw.polygon(
            [
                (screen_x[face[0]], screen_y[face[0]]),
                (screen_x[face[1]], screen_y[face[1]]),
                (screen_x[face[2]], screen_y[face[2]]),
            ],
            fill=color,
        )
    return image


def render_turntable(
    mesh: trimesh.Trimesh,
    out_dir: Path,
    stem: str = "turntable",
    size: int = 512,
    frames: int = 8,
) -> list[Path]:
    """渲 8 帧转台图，供人工挑选与后续 Critic 视觉通道使用。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared = _prepare(mesh)
    paths: list[Path] = []
    for index in range(max(1, frames)):
        angle = 2.0 * np.pi * index / max(1, frames)
        image = _render_frame(prepared, angle, size)
        path = out_dir / f"{stem}_{index:02d}.png"
        image.save(path)
        paths.append(path)
    return paths


def render_thumbnail(
    mesh: trimesh.Trimesh,
    path: Path,
    size: int = 256,
    angle: float = np.pi * 0.25,
) -> Path:
    """单张缩略图，资产库网格用这个。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = _render_frame(_prepare(mesh), angle, size)
    image.save(path)
    return path


def render_wireframe(
    mesh: trimesh.Trimesh,
    path: Path,
    size: int = 512,
    angle: float = np.pi * 0.25,
) -> Path:
    """线框预览 —— 看拓扑用的，比实心着色更能暴露三角面汤。"""
    prepared = _prepare(mesh)
    rotation = trimesh.transformations.rotation_matrix(angle, [0.0, 0.0, 1.0])
    vertices = trimesh.transformations.transform_points(
        np.asarray(prepared.vertices, dtype=np.float64), rotation
    )
    faces = np.asarray(prepared.faces, dtype=np.int64)

    margin = 0.08
    scale = size * (1.0 - 2 * margin) / 2.0
    center = size / 2.0
    screen_x = center + vertices[:, 0] * scale
    screen_y = center - vertices[:, 2] * scale

    image = Image.new("RGB", (size, size), (247, 246, 243))
    draw = ImageDraw.Draw(image)
    seen: set[tuple[int, int]] = set()
    for face in faces:
        for a in range(3):
            b = (a + 1) % 3
            key = (min(int(face[a]), int(face[b])), max(int(face[a]), int(face[b])))
            if key in seen:
                continue
            seen.add(key)
            draw.line(
                [
                    (screen_x[key[0]], screen_y[key[0]]),
                    (screen_x[key[1]], screen_y[key[1]]),
                ],
                fill=(90, 88, 84),
                width=1,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


__all__ = ["render_thumbnail", "render_turntable", "render_wireframe"]

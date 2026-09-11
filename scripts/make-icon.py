#!/usr/bin/env python3
"""生成 AssetAgent 应用图标（Windows .ico，内含多尺寸）。

设计：深青底色 + 白色等距立方体（三个面用不同透明度区分），
对应"把概念图变成 3D 资产"这件事。扁平、无渐变，小尺寸下也能认出来。

用法：python scripts/make-icon.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "apps" / "desktop" / "build-resources"
OUT_FILE = OUT_DIR / "icon.ico"

BG = (15, 110, 86, 255)  # c-teal 600
FACE_TOP = (255, 255, 255, 245)
FACE_LEFT = (255, 255, 255, 190)
FACE_RIGHT = (255, 255, 255, 130)

BASE_SIZE = 512
RADIUS_RATIO = 0.22
CUBE_RATIO = 0.30  # 立方体半径 / 画布边长


def cube_points(size: int) -> tuple[tuple[float, float], ...]:
    cx = cy = size / 2
    r = size * CUBE_RATIO
    h = r * math.sqrt(3) / 2
    return (
        (cx, cy - r),
        (cx + h, cy - r / 2),
        (cx + h, cy + r / 2),
        (cx, cy + r),
        (cx - h, cy + r / 2),
        (cx - h, cy - r / 2),
    )


def render(size: int) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=round(size * RADIUS_RATIO),
        fill=BG,
    )

    p0, p1, p2, p3, p4, p5 = cube_points(size)
    center = (size / 2, size / 2)

    faces = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    face_draw = ImageDraw.Draw(faces)
    face_draw.polygon([p0, p1, center, p5], fill=FACE_TOP)
    face_draw.polygon([p5, center, p3, p4], fill=FACE_LEFT)
    face_draw.polygon([p1, p2, p3, center], fill=FACE_RIGHT)

    canvas.alpha_composite(faces)
    return canvas


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = render(BASE_SIZE)
    base.save(
        OUT_FILE,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"已生成：{OUT_FILE}（{OUT_FILE.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

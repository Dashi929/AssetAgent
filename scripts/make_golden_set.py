"""生成黄金概念图集的占位图。

黄金集纪律（docs/ROADMAP.md）：W0 冻结 10 张概念图全程不得更换，作为回归基线。
真实美术概念图还没有 —— 这批占位图先把"考卷"定下来：文件名、构图类别、
难度覆盖都按黄金集的选取标准来，管线回归立即可跑。

美术出品后，**保持文件名不变**直接覆盖替换 PNG 即可无缝升级考卷；
替换后跑 `python -m app.golden` 确认基线仍通过。

用法：
    python scripts/make_golden_set.py            # 生成到 samples/golden/
    python scripts/make_golden_set.py --force    # 覆盖已存在的图（谨慎：考卷不能悄悄变）
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "samples" / "golden"

# (文件名, 类别标注, 绘制函数名) —— 顺序即考卷编号，冻结后不得增删改
GOLDEN_SET = [
    "golden_01_sword",        # 武器：细长结构
    "golden_02_shield",       # 圆形道具：曲面 + 徽记
    "golden_03_crate",        # 硬表面道具：木箱
    "golden_04_lantern",      # 复杂镂空：灯笼
    "golden_05_barrel",       # 圆柱陈设
    "golden_06_crystal",      # 晶体：多面体尖刺
    "golden_07_stump",        # 植被：树桩
    "golden_08_arch",         # 关卡灰盒：拱门
    "golden_09_anvil",        # 多部件：铁砧
    "golden_10_pot",          # 曲面：陶罐
]

SIZE = 512


def _font(size: int = 22):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 旧版 Pillow 的 load_default 不收 size
        return ImageFont.load_default()


def _canvas(title: str, bg=(236, 234, 228)) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (SIZE, SIZE), bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, SIZE - 56, SIZE, SIZE], fill=(226, 223, 216))
    draw.text((16, SIZE - 42), title, fill=(90, 88, 84), font=_font(20))
    return image, draw


def _sword(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.polygon([(cx, 70), (cx + 16, 110), (cx + 10, 300), (cx - 10, 300), (cx - 16, 110)], fill=(168, 172, 180))
    draw.rectangle([cx - 44, 300, cx + 44, 316], fill=(120, 96, 70))
    draw.rectangle([cx - 8, 316, cx + 8, 396], fill=(96, 74, 54))
    draw.ellipse([cx - 16, 396, cx + 16, 424], fill=(120, 96, 70))


def _shield(draw: ImageDraw.ImageDraw) -> None:
    cx, cy = SIZE / 2, 250
    draw.ellipse([cx - 130, cy - 150, cx + 130, cy + 150], fill=(140, 110, 76))
    draw.ellipse([cx - 100, cy - 118, cx + 100, cy + 118], outline=(104, 82, 58), width=10)
    draw.polygon([(cx, cy - 70), (cx + 60, cy), (cx, cy + 70), (cx - 60, cy)], fill=(196, 168, 118))
    draw.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(90, 74, 56))


def _crate(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle([120, 170, 400, 420], fill=(156, 122, 84))
    for y in (215, 260, 305, 350):
        draw.line([(126, y), (394, y)], fill=(128, 99, 66), width=8)
    draw.rectangle([120, 170, 400, 420], outline=(110, 86, 58), width=10)
    draw.line([(120, 170), (160, 130)], fill=(110, 86, 58), width=10)
    draw.line([(400, 170), (360, 130)], fill=(110, 86, 58), width=10)
    draw.line([(160, 130), (360, 130)], fill=(110, 86, 58), width=10)


def _lantern(draw: ImageDraw.ImageDraw) -> None:
    cx = int(SIZE / 2)
    draw.polygon([(cx - 30, 120), (cx + 30, 120), (cx + 22, 148), (cx - 22, 148)], fill=(70, 70, 74))
    draw.rectangle([cx - 52, 148, cx + 52, 330], fill=(84, 84, 90))
    for x in range(cx - 36, cx + 37, 18):
        draw.rectangle([x, 160, x + 8, 318], fill=(230, 196, 120))
    draw.polygon([(cx - 52, 330), (cx + 52, 330), (cx + 40, 372), (cx - 40, 372)], fill=(70, 70, 74))
    draw.ellipse([cx - 8, 92, cx + 8, 122], fill=(70, 70, 74))


def _barrel(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.rectangle([cx - 110, 140, cx + 110, 420], fill=(146, 112, 76))
    draw.ellipse([cx - 110, 116, cx + 110, 164], fill=(166, 132, 94))
    for y in (190, 280, 370):
        draw.rectangle([cx - 110, y, cx + 110, y + 14], fill=(96, 96, 100))
    draw.ellipse([cx - 88, 128, cx + 88, 152], outline=(120, 92, 60), width=6)


def _crystal(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.polygon([(cx, 90), (cx + 70, 220), (cx + 44, 400), (cx - 44, 400), (cx - 70, 220)], fill=(126, 176, 196))
    draw.polygon([(cx, 90), (cx + 70, 220), (cx, 250), (cx - 70, 220)], fill=(158, 202, 218))
    draw.polygon([(cx, 250), (cx + 44, 400), (cx, 420), (cx - 44, 400)], fill=(100, 148, 170))
    draw.polygon([(cx - 70, 220), (cx - 130, 320), (cx - 44, 400)], fill=(112, 160, 182))
    draw.polygon([(cx + 70, 220), (cx + 130, 320), (cx + 44, 400)], fill=(112, 160, 182))


def _stump(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.ellipse([cx - 120, 330, cx + 120, 430], fill=(96, 74, 54))
    draw.rectangle([cx - 120, 240, cx + 120, 380], fill=(118, 90, 64))
    draw.ellipse([cx - 120, 216, cx + 120, 264], fill=(188, 158, 122))
    draw.ellipse([cx - 84, 226, cx + 84, 256], outline=(150, 122, 90), width=6)
    draw.ellipse([cx - 40, 232, cx + 40, 250], outline=(150, 122, 90), width=4)
    draw.ellipse([cx - 150, 180, cx - 40, 240], fill=(108, 138, 88))
    draw.ellipse([cx + 40, 176, cx + 150, 236], fill=(96, 128, 80))


def _arch(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle([90, 200, 430, 430], fill=(168, 166, 160))
    draw.rectangle([200, 280, 320, 430], fill=(236, 234, 228))
    draw.polygon([(200, 280), (200, 240), (260, 200), (320, 240), (320, 280)], fill=(236, 234, 228))
    draw.rectangle([90, 170, 430, 210], fill=(184, 182, 176))


def _anvil(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.polygon([(cx - 150, 220), (cx + 90, 210), (cx + 130, 240), (cx + 90, 262), (cx - 150, 252)], fill=(84, 86, 92))
    draw.rectangle([cx - 60, 262, cx + 60, 300], fill=(104, 106, 112))
    draw.polygon([(cx - 60, 300), (cx + 60, 300), (cx + 40, 340), (cx - 40, 340)], fill=(84, 86, 92))
    draw.rectangle([cx - 100, 340, cx + 100, 380], fill=(96, 98, 104))
    draw.polygon([(cx + 90, 210), (cx + 170, 190), (cx + 150, 232), (cx + 130, 240)], fill=(84, 86, 92))


def _pot(draw: ImageDraw.ImageDraw) -> None:
    cx = SIZE / 2
    draw.ellipse([cx - 70, 130, cx + 70, 170], fill=(122, 90, 70))
    draw.ellipse([cx - 130, 180, cx + 130, 430], fill=(150, 112, 86))
    draw.ellipse([cx - 76, 136, cx + 76, 164], fill=(84, 62, 48))
    draw.arc([cx - 150, 170, cx - 70, 250], start=270, end=90, fill=(110, 82, 62), width=12)
    draw.arc([cx + 70, 170, cx + 150, 250], start=90, end=270, fill=(110, 82, 62), width=12)


_DRAWERS = {
    "golden_01_sword": "01 武器 · 细长结构",
    "golden_02_shield": "02 圆形道具 · 徽记",
    "golden_03_crate": "03 硬表面 · 木箱",
    "golden_04_lantern": "04 复杂镂空 · 灯笼",
    "golden_05_barrel": "05 圆柱陈设 · 木桶",
    "golden_06_crystal": "06 晶体 · 多面尖刺",
    "golden_07_stump": "07 植被 · 树桩",
    "golden_08_arch": "08 灰盒 · 拱门",
    "golden_09_anvil": "09 多部件 · 铁砧",
    "golden_10_pot": "10 曲面 · 陶罐",
}
_FUNCS = {
    "golden_01_sword": _sword,
    "golden_02_shield": _shield,
    "golden_03_crate": _crate,
    "golden_04_lantern": _lantern,
    "golden_05_barrel": _barrel,
    "golden_06_crystal": _crystal,
    "golden_07_stump": _stump,
    "golden_08_arch": _arch,
    "golden_09_anvil": _anvil,
    "golden_10_pot": _pot,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成黄金概念图集占位图")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--force", action="store_true", help="覆盖已存在的图（考卷不能悄悄变）")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in GOLDEN_SET:
        path = out / f"{name}.png"
        if path.exists() and not args.force:
            print(f"跳过（已存在）：{path}")
            continue
        image, draw = _canvas(_DRAWERS[name])
        _FUNCS[name](draw)
        image.save(path)
        print(f"生成：{path}")
    print(f"\n共 {len(GOLDEN_SET)} 张。跑回归：cd services/agent && .venv/Scripts/python -m app.golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

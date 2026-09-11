#!/usr/bin/env python3
"""编译 ufbx2obj（FBX → OBJ 原生转换器）。

用法：python services/agent/native/build.py

产物：services/agent/bin/ufbx2obj.exe（随仓库提交，开发直接可用；
打包时由 build-sidecar-only.py --add-binary 塞进 sidecar exe）。

依赖：任意 C11 编译器（Windows 上 MinGW gcc / MSVC cl 均可）。
ufbx 源码 vendored 在 native/ufbx/（MIT，master 0.23.0 快照）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
TARGET = BIN / ("ufbx2obj.exe" if sys.platform == "win32" else "ufbx2obj")


def main() -> int:
    BIN.mkdir(parents=True, exist_ok=True)

    if shutil.which("gcc"):
        cmd = [
            "gcc", "-O2", "-std=c11", "-Wall",
            "-I", str(HERE / "ufbx"),
            "-include", str(HERE / "win_shim.h"),
            "-o", str(TARGET),
            str(HERE / "fbx2obj.c"),
            str(HERE / "ufbx" / "ufbx.c"),
            "-lm",
        ]
    elif shutil.which("cl"):
        cmd = [
            "cl", "/O2", "/W3",
            "/I", str(HERE / "ufbx"),
            f"/Fo{BIN}\\",
            str(HERE / "fbx2obj.c"),
            str(HERE / "ufbx" / "ufbx.c"),
        ]
    else:
        print("找不到 C 编译器（gcc / cl）。")
        return 1

    print(">>", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        print(f"已生成：{TARGET}（{TARGET.stat().st_size / 1024:.0f} KB）")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())

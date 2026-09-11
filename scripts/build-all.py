#!/usr/bin/env python3
"""一键构建 AssetAgent Windows 安装包。

用法：
    cd scripts
    python build-all.py

输出：
    apps/desktop/release/AssetAgent Setup <版本>.exe

前置要求：
- Node.js + npm（已安装依赖）
- Python 3.11+（有 venv）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SIDEcar_DIR = REPO_ROOT / "services" / "agent"
DESKTOP_DIR = REPO_ROOT / "apps" / "desktop"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# 查找 npm（managed node 可能在非标准位置）
NPM_CMD = shutil.which("npm") or shutil.which("npm.cmd")
if not NPM_CMD:
    # fallback：检查常见 managed node 路径
    for candidate in [
        Path.home() / ".workbuddy-ai" / "binaries" / "node" / "versions" / "22.22.2-2" / "npm",
        Path.home() / ".workbuddy-ai" / "binaries" / "node" / "versions" / "22.22.2-2" / "npm.cmd",
        Path("C:/Program Files/nodejs/npm.cmd"),
    ]:
        if candidate.exists():
            NPM_CMD = str(candidate)
            break

if not NPM_CMD:
    print("找不到 npm。请安装 Node.js 并确保它在 PATH 中。")
    sys.exit(1)


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    """运行命令，失败时直接退出。"""
    print(f"\n>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        print(f"命令失败，退出码 {result.returncode}")
        sys.exit(result.returncode)


def install_pyinstaller() -> Path:
    """确保 PyInstaller 已安装在 venv 中。"""
    venv = SIDEcar_DIR / ".venv"
    if sys.platform == "win32":
        python = venv / "Scripts" / "python.exe"
        pip = venv / "Scripts" / "pip.exe"
    else:
        python = venv / "bin" / "python3"
        pip = venv / "bin" / "pip3"

    if not python.exists():
        print(f"找不到 venv：{python}")
        print("请先运行：cd services/agent && python -m venv .venv && pip install -e '.[mesh,dev]'")
        sys.exit(1)

    # 检查 PyInstaller 是否已安装
    check = subprocess.run([str(python), "-c", "import PyInstaller"], capture_output=True)
    if check.returncode != 0:
        print("安装 PyInstaller...")
        run([str(pip), "install", "pyinstaller>=6.0"])

    return python


def build_sidecar(python: Path) -> Path:
    """用 PyInstaller 打包 sidecar 为 .exe。"""
    print("\n=== 打包 sidecar ===")

    entry = SCRIPTS_DIR / "sidecar_entry.py"
    dist = DESKTOP_DIR / "sidecar-dist"
    work = DESKTOP_DIR / "sidecar-build"

    # 清理旧产物
    if dist.exists():
        shutil.rmtree(dist)
    if work.exists():
        shutil.rmtree(work)

    # 收集所有需要显式包含的隐藏导入
    hidden_imports = [
        "app.config",
        "app.models",
        "app.store",
        "app.jobs",
        "app.presets",
        "app.smoke",
        "app.routers.assets",
        "app.routers.files",
        "app.routers.jobs",
        "app.routers.meta",
        "app.routers.settings",
        "app.providers.base",
        "app.providers.mock",
        "app.providers.meshy",
        "app.providers.tripo",
        "app.providers.rodin",
        "app.providers.local_trellis",
        "app.providers.polling",
        "app.providers.registry",
        "app.tools.repair",
        "app.tools.decimate",
        "app.tools.uv",
        "app.tools.bake",
        "app.tools.export",
        "app.tools.render",
        "app.tools.validate",
        "app.tools.pipeline",
        "app.tools.blender",
        "app.tools.mesh_io",
        "app.tools.raster",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "email.mime.text",  # fastapi 内部用到
        "email.mime.multipart",
    ]

    cmd = [
        str(python), "-m", "PyInstaller",
        "--onefile",
        "--name", "assetagent-sidecar",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--clean",
        "--noconfirm",
    ]

    # 添加隐藏导入
    for mod in hidden_imports:
        cmd.extend(["--hidden-import", mod])

    # 添加数据文件（recipes 目录）
    recipes_src = REPO_ROOT / "recipes"
    if recipes_src.exists():
        cmd.extend(["--add-data", f"{recipes_src}{os.pathsep}recipes"])

    cmd.append(str(entry))

    run(cmd, cwd=REPO_ROOT)

    exe = dist / "assetagent-sidecar.exe"
    if not exe.exists():
        print(f"打包失败，找不到产物：{exe}")
        sys.exit(1)

    print(f"sidecar 已打包：{exe}")
    return exe


def build_desktop() -> None:
    """构建 Electron 前端和主进程。"""
    print("\n=== 构建桌面端 ===")

    # 1. 编译 Electron 主进程
    run([NPM_CMD, "run", "build:electron"], cwd=DESKTOP_DIR)

    # 2. Vite 生产构建
    run([NPM_CMD, "run", "build"], cwd=DESKTOP_DIR)


def package_installer() -> Path:
    """用 electron-builder 打包为安装程序。"""
    print("\n=== 打包安装程序 ===")

    run([NPM_CMD, "run", "dist"], cwd=DESKTOP_DIR)

    release_dir = DESKTOP_DIR / "release"
    installers = list(release_dir.glob("*.exe"))
    if not installers:
        print(f"找不到安装程序：{release_dir}")
        sys.exit(1)

    installer = max(installers, key=lambda p: p.stat().st_mtime)
    print(f"安装程序已生成：{installer}")
    return installer


def main() -> None:
    print("=" * 60)
    print("AssetAgent 构建脚本")
    print("=" * 60)

    # 1. 打包 sidecar
    python = install_pyinstaller()
    build_sidecar(python)

    # 2. 构建桌面端
    build_desktop()

    # 3. 打包安装程序
    installer = package_installer()

    print("\n" + "=" * 60)
    print(f"构建完成！")
    print(f"安装程序：{installer}")
    print("=" * 60)


if __name__ == "__main__":
    main()

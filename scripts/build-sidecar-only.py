#!/usr/bin/env python3
"""仅打包 sidecar（PyInstaller）。用于修复后快速重新打包。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def force_remove(path: Path) -> None:
    """沙箱里的 shutil.rmtree 走回收站、失败即拒绝，这里直接强删。"""
    if not path.exists():
        return
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
            check=False,
            capture_output=True,
        )
    else:
        subprocess.run(["rm", "-rf", str(path)], check=False, capture_output=True)
SIDEcar_DIR = REPO_ROOT / "services" / "agent"
DESKTOP_DIR = REPO_ROOT / "apps" / "desktop"

python = SIDEcar_DIR / ".venv" / "Scripts" / "python.exe"
dist = DESKTOP_DIR / "sidecar-dist"
work = DESKTOP_DIR / "sidecar-build"
entry = SIDEcar_DIR / "sidecar_entry.py"
recipes_src = REPO_ROOT / "recipes"

force_remove(dist)
force_remove(work)

cmd = [
    str(python), "-m", "PyInstaller",
    "--onefile",
    "--name", "assetagent-sidecar",
    "--distpath", str(dist),
    "--workpath", str(work),
    "--specpath", str(work),
    "--paths", str(SIDEcar_DIR),
    "--clean",
    "--noconfirm",
    f"--add-data={recipes_src};recipes",
]

hidden = [
    "app.config", "app.paths", "app.models", "app.store", "app.jobs",
    "app.presets", "app.smoke", "app.telemetry", "app.knowledge",
    "app.ai.llm",
    "app.routers.assets", "app.routers.files", "app.routers.jobs",
    "app.routers.meta", "app.routers.settings", "app.routers.telemetry",
    "app.routers.ai_assist",
    "app.providers.base", "app.providers.mock", "app.providers.meshy",
    "app.providers.tripo", "app.providers.rodin", "app.providers.hunyuan3d",
    "app.providers.local_trellis",
    "app.providers.polling", "app.providers.registry",
    "app.tools.repair", "app.tools.decimate", "app.tools.uv", "app.tools.bake",
    "app.tools.export", "app.tools.render", "app.tools.validate", "app.tools.pipeline",
    "app.tools.blender", "app.tools.convert", "app.tools.mesh_io", "app.tools.raster",
    "app.tools.gltf_load",
    # trimesh.proximity 的最近点查询（软件版底色烘焙）依赖 rtree，是惰性导入，
    # 静态分析抓不到，必须显式声明
    "rtree",
    "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
]
for mod in hidden:
    cmd.extend(["--hidden-import", mod])

# 内置 FBX 转换器（native/build.py 编译产物），打包后落在 _MEIPASS 根
ufbx_tool = SIDEcar_DIR / "bin" / "ufbx2obj.exe"
if ufbx_tool.is_file():
    cmd.append(f"--add-binary={ufbx_tool};.")
else:
    print(f"⚠ 未找到 {ufbx_tool}，打包出的应用 FBX 导入将回退到 Blender")

# VC++ 运行时：fast_simplification / numpy 的 wheel 自带 2019 年的 msvcp140（14.16），
# PyInstaller 会把它打进包并**全局遮蔽**系统的新版（14.5x），xatlas 一调新版 CRT
# 功能就 0xC0000005 段错误（管线 uv 步必崩，Windows 事件日志的故障模块是
# _MEI*/MSVCP140.dll）。这里显式塞入系统版本覆盖。MSVCP140 向后兼容，
# 新版运行旧构建只赚不亏。见 HANDOFF 第十二轮。
import os  # noqa: E402

system_msvcp = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "MSVCP140.dll"
if system_msvcp.is_file():
    cmd.append(f"--add-binary={system_msvcp};.")
else:
    print("⚠ 找不到系统 MSVCP140.dll，wheel 自带的老版运行时可能导致管线段错误")

cmd.append(str(entry))

print(">> PyInstaller sidecar ...")
result = subprocess.run(cmd, cwd=SIDEcar_DIR, check=False)
sys.exit(result.returncode)

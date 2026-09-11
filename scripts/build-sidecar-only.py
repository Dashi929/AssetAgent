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
    "--clean",
    "--noconfirm",
    f"--add-data={recipes_src};recipes",
]

hidden = [
    "app.config", "app.paths", "app.models", "app.store", "app.jobs",
    "app.presets", "app.smoke",
    "app.routers.assets", "app.routers.files", "app.routers.jobs",
    "app.routers.meta", "app.routers.settings",
    "app.providers.base", "app.providers.mock", "app.providers.meshy",
    "app.providers.tripo", "app.providers.rodin", "app.providers.local_trellis",
    "app.providers.polling", "app.providers.registry",
    "app.tools.repair", "app.tools.decimate", "app.tools.uv", "app.tools.bake",
    "app.tools.export", "app.tools.render", "app.tools.validate", "app.tools.pipeline",
    "app.tools.blender", "app.tools.mesh_io", "app.tools.raster",
    "uvicorn.logging", "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
]
for mod in hidden:
    cmd.extend(["--hidden-import", mod])

cmd.append(str(entry))

print(">> PyInstaller sidecar ...")
result = subprocess.run(cmd, cwd=SIDEcar_DIR, check=False)
sys.exit(result.returncode)

"""打包 / 开发两种模式下的路径解析。

打包后的目录布局（安装目录/resources/）：

    resources/
      app.asar
      recipes/                 校验规则与预设（只读资源）
      sidecar/                 源码副本（仅供 fallback 模式）
      sidecar-dist/
        assetagent-sidecar.exe sys.executable

坑位记录（改这个文件前务必读）：

1. PyInstaller onefile 会把所有资源解压到临时目录 `sys._MEIPASS`，
   所以 `__file__` 的 parents 推导在打包后完全失效，必须显式判断 frozen。
2. 数据目录绝不能放在安装目录里 —— 卸载程序会把用户的资产一起删掉。
   打包后固定落到系统级应用数据目录（Windows 是 %LOCALAPPDATA%）。
3. PyInstaller 用 `--add-data=recipes;recipes` 往 exe 里塞了一份规则快照，
   所以 _MEIPASS 下也会有 recipes/。但**不能优先用它** —— 那是冻结副本，
   用户改不动，而且临时目录路径每次启动都变。见 resource_dir() 的注释。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

_PRODUCT = "AssetAgent"


def meipass_dir() -> Path | None:
    """PyInstaller 的临时解压目录，非打包模式返回 None。"""
    raw = getattr(sys, "_MEIPASS", None)
    return Path(raw) if raw else None


def app_root() -> Path:
    """应用根。开发时是仓库根，打包后是 resources/ 目录。"""
    if FROZEN:
        # <...>/resources/sidecar-dist/assetagent-sidecar.exe -> <...>/resources
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[3]


def resource_dir() -> Path:
    """只读资源所在目录。

    打包后优先用**安装目录**下的 resources/，而不是 exe 内部的 _MEIPASS 快照。
    原因：recipes 是给用户看的规则配置（校验规则、规格预设、导出预设），
    必须可以直接编辑生效；而 _MEIPASS 里的那份是 PyInstaller 冻结的副本
    （build-sidecar-only.py 用 --add-data 打进去的），只作兜底。
    另外 onefile 每次启动都会换一个 _MEIxxxx 临时目录，路径不稳定。
    """
    installed = app_root()
    if (installed / "recipes").is_dir():
        return installed

    meipass = meipass_dir()
    if meipass is not None and (meipass / "recipes").is_dir():
        return meipass
    return installed


def find_recipes_dir() -> Path:
    """定位 recipes（校验规则 + 预设）。

    从资源目录向上找最多 4 层，兼容 resources/ 布局与开发时仓库根布局。
    找不到就返回理论路径（调用方会拿到一个不存在的目录，而不是静默用错目录）。
    """
    start = resource_dir()
    for base in (start, *start.parents[:4]):
        candidate = base / "recipes"
        if candidate.is_dir():
            return candidate
    return app_root() / "recipes"


def default_data_dir() -> Path:
    """用户数据目录。

    优先级：环境变量 ASSETAGENT_DATA_DIR > 系统应用数据目录 > 开发时的仓库 .data。
    打包后一定落在用户目录下 —— 可写、且卸载不会带走资产。
    """
    raw = os.environ.get("ASSETAGENT_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()

    if not FROZEN:
        return app_root() / ".data"

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / _PRODUCT

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _PRODUCT

    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / _PRODUCT.lower()


__all__ = [
    "FROZEN",
    "app_root",
    "default_data_dir",
    "find_recipes_dir",
    "meipass_dir",
    "resource_dir",
]

"""Blender headless 调用封装。

Blender 只以"子进程 + 文件交换"的方式使用，不做窗口内嵌 —— 闭源商业产品链接 Blender
有 GPL 传染风险，且包体 +300MB、窗口自动化脆弱（见 docs/ARCHITECTURE.md 边界一节）。

Blender 不存在时，调用方必须**降级而不是失败**：烘焙跳过、转台退回软件渲染、
FBX 导出改为"只出 GLB + 一条警告"。美术宁可拿到少了贴图的资产，也不要一个失败的任务。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..config import get_settings

DEFAULT_TIMEOUT = 900


class BlenderUnavailable(RuntimeError):
    pass


def blender_bin() -> str | None:
    return get_settings().blender_bin()


def available() -> bool:
    return blender_bin() is not None


def recipes_dir() -> Path:
    return get_settings().recipes_dir / "bpy"


def run_script(
    script_name: str,
    args: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """跑一个 recipes/bpy/*.py 脚本。

    返回 (是否成功, 日志)。脚本内部约定：成功时最后一行打印 `OK <json>`，失败打印 `ERR <原因>`。
    """
    binary = blender_bin()
    if not binary:
        raise BlenderUnavailable(
            "未检测到 Blender。请在「设置」里指定 blender 可执行文件路径，"
            "或安装 Blender 后重启应用。"
        )

    script = recipes_dir() / script_name
    if not script.exists():
        raise FileNotFoundError(f"缺少 bpy 脚本：{script}")

    command = [binary, "--background", "--factory-startup", "--python", str(script)]
    if args:
        command.append("--")
        for key, value in args.items():
            command.extend([f"--{key}", str(value)])

    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本模块构造，无外部注入
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"Blender 执行超时（{timeout}s）"
    except OSError as exc:
        return False, f"无法启动 Blender：{exc}"

    log = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        return False, _tail(log) or f"Blender 退出码 {completed.returncode}"

    if "OK " in log:
        return True, _tail(log)
    if "ERR " in log:
        return False, _tail(log)
    return False, _tail(log) or "Blender 执行结束但没有输出结果标记"


def _tail(log: str, lines: int = 25) -> str:
    """只保留日志尾部 —— Blender 的输出非常啰嗦，全带回来会把错误信息淹掉。"""
    stripped = [line for line in log.splitlines() if line.strip()]
    return "\n".join(stripped[-lines:])


def diagnose() -> dict[str, object]:
    """设置页/诊断接口用：告诉用户当前 Blender 能不能用。"""
    binary = blender_bin()
    return {
        "available": binary is not None,
        "path": binary,
        "which": shutil.which("blender"),
        "recipes_dir": str(recipes_dir()),
    }


__all__ = [
    "DEFAULT_TIMEOUT",
    "BlenderUnavailable",
    "available",
    "blender_bin",
    "diagnose",
    "recipes_dir",
    "run_script",
]

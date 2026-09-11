"""管线步骤 5：烘焙。

从原始高模把 normal / AO / curvature 烘到减面后的低模上。
必须走 Blender（烘焙是 bpy 的活，纯 Python 做不了），所以没有 Blender 时这一步会 skip。

skip 而不是 fail 是刻意的：没有烘焙的资产仍然可以用（只是细节丢了），
而一个失败的任务会让美术丢掉整条链路的结果。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .blender import BlenderUnavailable, available, run_script


def bake_textures(
    low_mesh: Path,
    high_mesh: Path | None,
    out_dir: Path,
    resolution: int = 2048,
    maps: tuple[str, ...] = ("normal", "ao"),
) -> dict[str, Any]:
    """把高模细节烘到低模。返回报告 dict（status = ok / skipped）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "skipped",
        "method": "blender-bpy",
        "resolution": resolution,
        "maps": list(maps),
        "files": [],
        "reason": None,
    }

    if not available():
        report["reason"] = (
            "未检测到 Blender，已跳过烘焙。资产仍可导出，但会缺少 normal/AO 贴图；"
            "安装 Blender 或在设置里指定其路径后重新跑管线即可补上。"
        )
        return report

    if high_mesh is None or not Path(high_mesh).exists():
        report["reason"] = "没有可用于烘焙的高模参考，已跳过烘焙。"
        return report

    try:
        success, log = run_script(
            "bake_textures.py",
            {
                "low": str(low_mesh),
                "high": str(Path(high_mesh)),
                "out": str(out_dir),
                "resolution": str(resolution),
                "maps": ",".join(maps),
            },
        )
    except BlenderUnavailable as exc:
        report["reason"] = str(exc)
        return report

    report["log"] = log
    if not success:
        report["reason"] = f"烘焙失败：{log}"
        return report

    report["status"] = "ok"
    report["files"] = [str(p) for p in sorted(out_dir.glob("*.png"))]
    if not report["files"]:
        report["status"] = "skipped"
        report["reason"] = "Blender 报告成功，但没有产出任何贴图文件。"
    return report


__all__ = ["bake_textures"]

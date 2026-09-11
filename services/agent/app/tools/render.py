"""管线步骤 8：转台渲染。

优先 Blender（画质好、有 HDRI），没有就退回内置软渲染（raster.py）。
两条路都必须能出图 —— "资产库有缩略图、变体挑选有转台图"是 MVP 的硬要求，
不能因为某台机器没装 Blender 就整块功能消失。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import trimesh

from . import raster
from .blender import BlenderUnavailable, available, run_script


def render_turntable(
    mesh: trimesh.Trimesh,
    mesh_path: Path,
    out_dir: Path,
    frames: int = 8,
    size: int = 512,
    prefer_blender: bool = True,
) -> dict[str, Any]:
    """渲转台图。返回 {files, method, reason}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"files": [], "method": "", "reason": None}

    if prefer_blender and available():
        try:
            success, log = run_script(
                "render_turntable.py",
                {
                    "mesh": str(mesh_path),
                    "out": str(out_dir),
                    "frames": str(frames),
                    "size": str(size),
                },
            )
        except BlenderUnavailable as exc:
            success, log = False, str(exc)

        if success:
            files = sorted(out_dir.glob("turntable_*.png"))
            if files:
                report["files"] = [str(p) for p in files]
                report["method"] = "blender-bpy"
                return report
        report["reason"] = f"Blender 渲染未成功，已退回内置软渲染。（{log[:200]}）"

    # 软渲染兜底
    try:
        files = raster.render_turntable(mesh, out_dir, size=size, frames=frames)
    except Exception as exc:
        report["reason"] = f"软渲染也失败了：{type(exc).__name__}: {exc}"
        return report

    report["files"] = [str(p) for p in files]
    report["method"] = "software-raster"
    return report


def render_thumbnail(mesh: trimesh.Trimesh, path: Path, size: int = 256) -> dict[str, Any]:
    """资产库缩略图。软渲染足够快（毫秒级），没必要为它启动 Blender。"""
    try:
        output = raster.render_thumbnail(mesh, path, size=size)
    except Exception as exc:
        return {"path": None, "method": "none", "reason": f"{type(exc).__name__}: {exc}"}
    return {"path": str(output), "method": "software-raster", "reason": None}


__all__ = ["render_thumbnail", "render_turntable"]

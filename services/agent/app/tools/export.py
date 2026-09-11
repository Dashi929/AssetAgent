"""管线步骤 7：导出。

GLB 用 trimesh 直接写（不需要 Blender），FBX 必须走 Blender。
没有 Blender 时**不是失败**，而是"出 GLB + 一条明确警告" ——
Unity/UE 都能直接吃 GLB，美术不会因为缺 FBX 就卡住。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import trimesh

from ..presets import get_export_preset
from .blender import BlenderUnavailable, available, run_script
from .mesh_io import MeshError, save_mesh

INVALID_CHARS = re.compile(r"[^A-Za-z0-9_\-]+")


def sanitize_name(name: str, fallback: str = "Asset") -> str:
    """把资产名压成引擎友好的文件名。中文名会被压掉，所以保底给 fallback。"""
    cleaned = INVALID_CHARS.sub("_", (name or "").strip()).strip("_")
    return cleaned or fallback


def export_asset(
    mesh: trimesh.Trimesh,
    out_dir: Path,
    preset_name: str,
    asset_name: str = "Asset",
    texture_files: list[Path] | None = None,
    source_mesh_path: Path | None = None,
) -> dict[str, Any]:
    """按引擎预设导出。返回 {files, warnings, preset, manifest}。"""
    preset = get_export_preset(preset_name)
    formats: list[str] = list(preset.get("formats") or ["glb"])
    stem = sanitize_name(asset_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    warnings: list[str] = []

    # 1) GLB —— 无条件导出。它既是前端预览的基准格式，也是通用交付格式，
    #    所以哪怕预设里没列（比如将来的纯 FBX 预设），也照样出一份。
    try:
        glb_path = save_mesh(mesh, out_dir / f"{stem}.glb")
        files.append(str(glb_path))
    except MeshError as exc:
        warnings.append(f"GLB 导出失败：{exc}")

    # 2) FBX —— 需要 Blender
    if "fbx" in formats:
        if available() and source_mesh_path is not None:
            try:
                success, log = run_script(
                    "export_asset.py",
                    {
                        "mesh": str(source_mesh_path),
                        "out": str(out_dir / f"{stem}.fbx"),
                        "up_axis": str(preset.get("up_axis", "Y")),
                        "unit_scale": str(preset.get("unit_scale", 1.0)),
                    },
                )
            except BlenderUnavailable as exc:
                success, log = False, str(exc)
            if success and (out_dir / f"{stem}.fbx").exists():
                files.append(str(out_dir / f"{stem}.fbx"))
            else:
                warnings.append(
                    f"FBX 导出未完成（{preset.get('display_name')} 预设）：{log[:200]}"
                )
        else:
            warnings.append(
                "未检测到 Blender，已跳过 FBX 导出，只产出 GLB。"
                "Unity / Unreal 均可直接导入 GLB；如需 FBX，请安装 Blender 后重新导出。"
            )

    # 3) 贴图包
    texture_dir = out_dir / "textures"
    copied: list[str] = []
    for texture in texture_files or []:
        texture_dir.mkdir(parents=True, exist_ok=True)
        target = texture_dir / texture.name
        shutil.copy2(texture, target)
        copied.append(str(target))
    files.extend(copied)
    if not copied:
        warnings.append("该版本没有贴图文件，导出的资产只有几何体。")

    # 4) glTF 坐标系提示：GLB 恒为 Y-up / 米制，UE 需要 Z-up / 厘米，导入时要注意
    if str(preset.get("up_axis", "Y")).upper() == "Z":
        warnings.append(
            "GLB 遵循 glTF 规范恒为 Y-up / 米制，与 Unreal 的 Z-up / 厘米不一致，"
            "导入 UE 时请使用 FBX 或手动设置导入选项。"
        )

    manifest = {
        "asset_name": asset_name,
        "file_stem": stem,
        "preset": preset.get("key", preset_name),
        "display_name": preset.get("display_name", preset_name),
        "up_axis": preset.get("up_axis"),
        "unit": preset.get("unit"),
        "unit_scale": preset.get("unit_scale"),
        "formats": formats,
        "files": files,
        "textures": copied,
        "warnings": warnings,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append(str(manifest_path))

    return {
        "files": files,
        "warnings": warnings,
        "preset": preset.get("key", preset_name),
        "manifest": manifest,
        "manifest_path": str(manifest_path),
    }


__all__ = ["export_asset", "sanitize_name"]

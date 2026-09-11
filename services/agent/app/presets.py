"""规格预设与导出预设的加载。

预设放在 recipes/presets/*.yaml 而不是代码里，是为了让技术美术能直接改阈值和引擎规范，
不用碰 Python —— 这是 T3 轨道的落点。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings
from .models import SpecPreset

DEFAULT_EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "unity": {
        "display_name": "Unity",
        "up_axis": "Y",
        "unit": "meter",
        "unit_scale": 1.0,
        "formats": ["glb", "fbx"],
        "naming": {"mesh": r"^SM_[A-Za-z0-9_]+$", "texture": r"^T_[A-Za-z0-9_]+$"},
        "texture": {"metallic_roughness": "unity_standard"},
    },
    "unreal": {
        "display_name": "Unreal",
        "up_axis": "Z",
        "unit": "centimeter",
        "unit_scale": 100.0,
        "formats": ["glb", "fbx"],
        "naming": {"mesh": r"^SM_[A-Za-z0-9_]+$", "texture": r"^T_[A-Za-z0-9_]+$"},
        "texture": {"metallic_roughness": "orm"},
    },
    "generic": {
        "display_name": "通用 glTF",
        "up_axis": "Y",
        "unit": "meter",
        "unit_scale": 1.0,
        "formats": ["glb"],
        "naming": {},
        "texture": {"metallic_roughness": "gltf_native"},
    },
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


@lru_cache(maxsize=1)
def _spec_presets_raw() -> dict[str, Any]:
    return _read_yaml(get_settings().recipes_dir / "presets" / "spec_presets.yaml")


def load_spec_presets() -> dict[str, SpecPreset]:
    """返回 {key: SpecPreset}。YAML 缺失时给一套能用的默认值，保证前端不空。"""
    raw = (_spec_presets_raw().get("presets") or {})
    presets: dict[str, SpecPreset] = {}
    for key, value in raw.items():
        try:
            presets[str(key)] = SpecPreset(**value)
        except Exception:
            continue
    if not presets:
        presets = {
            "prop_default": SpecPreset(name="道具 · 默认", category="prop"),
            "blocker": SpecPreset(
                name="关卡灰盒 Blocker",
                category="blocker",
                face_budget=1500,
                want_quads=False,
                texture_resolution=1024,
            ),
        }
    return presets


def load_export_presets() -> dict[str, dict[str, Any]]:
    """返回 {key: preset}。缺项用内置默认补，不让前端因为配置缺失而崩。"""
    raw = (_read_yaml(get_settings().recipes_dir / "presets" / "export_presets.yaml").get("presets") or {})
    presets = {key: dict(value) for key, value in DEFAULT_EXPORT_PRESETS.items()}
    for key, value in raw.items():
        presets.setdefault(str(key), {}).update(value or {})
    return presets


def get_export_preset(name: str) -> dict[str, Any]:
    presets = load_export_presets()
    key = (name or "generic").strip().lower()
    if key not in presets:
        known = "、".join(sorted(presets))
        raise KeyError(f"未知的导出预设「{name}」。可用：{known}")
    preset = dict(presets[key])
    preset["key"] = key
    return preset


def reload_cache() -> None:
    _spec_presets_raw.cache_clear()


__all__ = [
    "DEFAULT_EXPORT_PRESETS",
    "get_export_preset",
    "load_export_presets",
    "load_spec_presets",
    "reload_cache",
]

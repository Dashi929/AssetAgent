"""健康检查、预设、诊断。

诊断接口是给"美术装不上东西"这类问题准备的：一眼看出 Blender 有没有、减面后端装没装、
数据目录在哪。少问一轮"你那边装了啥"。
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException

from .. import __version__
from ..config import get_settings
from ..presets import load_export_presets, load_spec_presets
from ..providers import ProviderError, registry
from ..tools import blender_available, decimate_backend

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "python": sys.version.split()[0]}


@router.get("/presets")
async def presets() -> dict:
    return {
        "spec_presets": {k: v.model_dump(mode="json") for k, v in load_spec_presets().items()},
        "export_presets": load_export_presets(),
    }


@router.get("/estimate")
async def estimate(provider: str = "", variants: int = 1) -> dict:
    """生成前精确预估：按 Provider 自己的算法算（离线占位/本地模型报 ¥0，不报假价格）。"""
    count = max(1, min(6, variants))
    try:
        resolved = registry.resolve(provider or None)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    settings = get_settings()
    return {
        "provider": resolved.name,
        "variants": count,
        "estimate_cny": resolved.estimate_cost(count, settings.cost_per_generation),
    }


@router.get("/diagnostics")
async def diagnostics() -> dict:
    settings = get_settings()
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "data_dir": str(settings.data_dir),
        "recipes_dir": str(settings.recipes_dir),
        "route_mode": str(settings.effective("route_mode", "byok")),
        "blender": {
            "available": blender_available(),
            "path": settings.blender_bin(),
        },
        "mesh_backends": {
            "decimate": decimate_backend(),
            "uv": _uv_backend(),
        },
        "providers": [info.model_dump(mode="json") for info in registry.list_info()],
    }


def _uv_backend() -> str:
    try:
        import xatlas  # type: ignore  # noqa: F401

        return "xatlas"
    except ImportError:
        return "未安装"

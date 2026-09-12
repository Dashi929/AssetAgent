"""设置接口：BYOK Key 管理、预算、路由开关、用量台账。

安全约定：接口**永远不返回完整 Key**，只回掩码和"是否已配置"。
自填 Key 只写本机（data/secrets.json，已 gitignore），永不上传。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings, reset_settings_cache
from ..models import ByokBody, SettingsBody
from ..providers import ProviderError, registry

router = APIRouter(prefix="/api/settings", tags=["settings"])

MASK_TAIL = 4


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= MASK_TAIL:
        return "*" * len(key)
    return "*" * (len(key) - MASK_TAIL) + key[-MASK_TAIL:]


def _snapshot() -> dict:
    settings = get_settings()
    providers = []
    for info in registry.list_info():
        providers.append(
            {
                **info.model_dump(mode="json"),
                "key_masked": _mask(settings.provider_key(info.name)),
            }
        )
    month = dt.datetime.now().strftime("%Y-%m")
    llm_key = settings.provider_key("llm")
    return {
        "route_mode": str(settings.effective("route_mode", "byok")),
        "allow_mock_fallback": bool(settings.effective("allow_mock_fallback", True)),
        "monthly_budget_cny": float(settings.effective("monthly_budget_cny", 200.0) or 0),
        "cost_per_generation_cny": settings.cost_per_generation,
        "default_face_budget": settings.default_face_budget,
        "blender_bin": str(settings.effective("blender_bin", "") or ""),
        "data_dir": str(settings.data_dir),
        "providers": providers,
        "llm": {
            "base_url": str(settings.effective("llm_base_url", "") or ""),
            "model": str(settings.effective("llm_model", "") or ""),
            "configured": bool(llm_key),
            "key_masked": _mask(llm_key),
        },
        "usage": {
            "month": month,
            "this_month": settings.usage_summary(month=month),
            "all_time": settings.usage_summary(),
        },
    }


@router.get("")
async def get_settings_snapshot() -> dict:
    return _snapshot()


@router.patch("")
async def patch_settings(body: SettingsBody) -> dict:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if patch:
        get_settings().patch_runtime(patch)
        reset_settings_cache()
        registry.reload()
    return _snapshot()


@router.put("/byok")
async def set_byok_key(body: ByokBody) -> dict:
    settings = get_settings()
    name = body.provider.strip().lower()
    if name not in {info.name for info in registry.list_info()}:
        raise HTTPException(status_code=400, detail=f"未知的 Provider：{body.provider}")
    if name in {"mock", "local_trellis"}:
        raise HTTPException(status_code=400, detail=f"「{name}」不需要 API Key。")
    settings.set_provider_key(name, body.key.strip())
    reset_settings_cache()
    registry.reload()
    return _snapshot()


@router.delete("/byok/{provider}")
async def clear_byok_key(provider: str) -> dict:
    get_settings().set_provider_key(provider, "")
    reset_settings_cache()
    registry.reload()
    return _snapshot()


@router.post("/byok/{provider}/test")
async def test_provider(provider: str) -> dict:
    """连通性自检 —— 设置页那个"测试连接"按钮。"""
    try:
        instance = registry.get(provider)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not instance.has_key:
        return {"provider": provider, "ok": False, "message": "还没有填写 API Key。"}

    try:
        ok = await instance.healthcheck()
    except Exception as exc:
        return {"provider": provider, "ok": False, "message": f"{type(exc).__name__}: {exc}"}

    return {
        "provider": provider,
        "ok": ok,
        "message": "连接正常，Key 可用。" if ok else "连接失败。请检查 Key 是否正确、网络是否可达。",
    }


@router.get("/usage")
async def usage(month: str | None = None) -> dict:
    return get_settings().usage_summary(month=month)


# ------------------------------------------------------------------ AI 助手（LLM）


class LlmConfigBody(BaseModel):
    """AI 助手配置：base_url / model / key 任选其一更新。"""

    base_url: str | None = None
    model: str | None = None
    key: str | None = None


@router.put("/llm")
async def set_llm_config(body: LlmConfigBody) -> dict:
    settings = get_settings()
    patch = {k: v for k, v in {"llm_base_url": body.base_url, "llm_model": body.model}.items() if v}
    if patch:
        settings.patch_runtime(patch)
    if body.key is not None:
        settings.set_provider_key("llm", body.key.strip())
    reset_settings_cache()
    return _snapshot()


@router.post("/llm/test")
async def test_llm() -> dict:
    """AI 助手连通性自检。"""
    from ..ai import llm as llm_module

    if not llm_module.llm_configured():
        return {"ok": False, "message": "还没有填写 AI 助手的 API Key。"}
    ok, message = await llm_module.healthcheck()
    return {"ok": ok, "message": message}

"""2D 图片生成适配器（CogView，OpenAI 兼容 images API）。

与 LLM 助手同一把 Key / 同一个平台（默认智谱开放平台），换 base_url 也能接
其它兼容服务。生成结果落盘为 PNG，供 2D 素材资产使用。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..config import get_settings
from .llm import LLMError

TIMEOUT = 180.0


class ImageGenError(LLMError):
    """图片生成的可预期错误，消息透给前端。"""


def imagegen_model() -> str:
    return str(get_settings().effective("imagegen_model", "cogview-3-flash") or "cogview-3-flash")


async def generate_image(prompt: str, out_path: Path, *, size: str = "1024x1024") -> Path:
    """生成一张图片并落盘。返回文件路径。"""
    settings = get_settings()
    key = settings.provider_key("llm")
    if not key:
        raise ImageGenError("还没有配置 AI 助手的 API Key。请到「设置 → AI 助手」填写。")
    base = str(settings.effective("llm_base_url", "") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.post(
                f"{base}/images/generations",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": imagegen_model(),
                    "prompt": prompt,
                    "size": size,
                    "response_format": "b64_json",
                },
            )
        except httpx.HTTPError as exc:
            raise ImageGenError(f"连不上图片生成服务（{base}）：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300]
        if response.status_code in (401, 403):
            raise ImageGenError(f"Key 无效或没有图片生成权限（HTTP {response.status_code}）：{detail}")
        raise ImageGenError(f"图片生成失败（HTTP {response.status_code}）：{detail}")

    try:
        payload = response.json()["data"][0]
    except (KeyError, IndexError, ValueError) as exc:
        raise ImageGenError(f"图片生成返回格式异常：{response.text[:200]}") from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if payload.get("b64_json"):
        out_path.write_bytes(base64.b64decode(payload["b64_json"]))
    elif payload.get("url"):
        url = payload["url"]
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            dl = await client.get(url)
        if dl.status_code >= 400:
            raise ImageGenError(f"下载生成图片失败（HTTP {dl.status_code}）")
        out_path.write_bytes(dl.content)
    else:
        raise ImageGenError("图片生成服务既没返回图片数据也没返回链接。")

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise ImageGenError("图片生成落盘失败。")
    return out_path

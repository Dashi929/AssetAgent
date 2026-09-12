"""OpenAI 兼容 LLM 适配器（chat + vision + JSON 模式）。

模型无关：只要服务实现了 POST {base_url}/chat/completions 就能接
（智谱 GLM / DeepSeek / Qwen 兼容模式 / Moonshot / OpenAI 官方等）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"  # 智谱开放平台（国内可达）
DEFAULT_MODEL = "glm-4-flash"
TIMEOUT = 120.0


class LLMError(RuntimeError):
    """LLM 调用层的可预期错误，消息透给前端。"""


def llm_configured() -> bool:
    return bool(get_settings().provider_key("llm"))


def _headers() -> dict[str, str]:
    key = get_settings().provider_key("llm")
    if not key:
        raise LLMError("还没有配置 AI 助手的 API Key。请到「设置 → AI 助手」填写。")
    return {"Authorization": f"Bearer {key}"}


def _base_url() -> str:
    return str(get_settings().effective("llm_base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return str(get_settings().effective("llm_model", DEFAULT_MODEL) or DEFAULT_MODEL)


async def chat(
    messages: list[dict[str, Any]],
    *,
    json_mode: bool = False,
    temperature: float = 0.6,
    timeout: float = TIMEOUT,
) -> str:
    """发起一次 chat 补全，返回助手回复文本。"""
    body: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(
                f"{_base_url()}/chat/completions", headers=_headers(), json=body
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"连不上 LLM 服务（{_base_url()}）：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300]
        if response.status_code in (401, 403):
            raise LLMError(f"LLM Key 无效或没有权限（HTTP {response.status_code}）：{detail}")
        raise LLMError(f"LLM 调用失败（HTTP {response.status_code}）：{detail}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"LLM 返回格式异常：{response.text[:200]}") from exc
    if not content:
        raise LLMError("LLM 返回了空回复。")
    return str(content)


def image_content(text: str, image_paths: list[Path | str]) -> list[dict[str, Any]]:
    """构造带图片的 user 消息 content（vision 模型用）。"""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for raw in image_paths:
        path = Path(raw)
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(path.suffix.lower(), "image/png")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return content


async def vision_chat_json(
    system: str, user_text: str, image_paths: list[Path | str], *, temperature: float = 0.2
) -> dict[str, Any]:
    """带图的 JSON 问答：概念图/渲染图 → 结构化结论。"""
    text = await chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": image_content(user_text, image_paths)},
        ],
        json_mode=True,
        temperature=temperature,
    )
    return parse_json(text)


async def chat_json(system: str, user_text: str, *, temperature: float = 0.4) -> dict[str, Any]:
    """纯文本 JSON 问答。"""
    text = await chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        json_mode=True,
        temperature=temperature,
    )
    return parse_json(text)


def parse_json(text: str) -> dict[str, Any]:
    """解析模型回复里的 JSON 对象 —— 容忍 markdown 围栏与前后废话。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("`" * 3, 2)[1] if "```" in cleaned else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"LLM 没有返回 JSON：{text[:200]}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM 返回的 JSON 解析失败：{text[:200]}") from exc


async def healthcheck() -> tuple[bool, str]:
    """设置页「测试连接」：发一句 ping，验证 base_url/model/key 都对。"""
    try:
        reply = await chat(
            [{"role": "user", "content": "回复两个字：正常"}],
            temperature=0.0,
            timeout=20.0,
        )
    except LLMError as exc:
        return False, str(exc)
    return True, f"连接正常（模型回复：{reply[:30].strip()}）"

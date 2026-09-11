"""异步任务轮询的共用工具。

三家 Provider 都是"提交任务 → 轮询状态 → 拿下载链接"的异步模式，
把这段抽出来，避免三份几乎相同的轮询代码各自长歪。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .base import ProviderError

DONE_STATES = {"succeeded", "success", "done", "completed", "finished"}
FAIL_STATES = {"failed", "failure", "error", "cancelled", "canceled", "expired"}


def dig(payload: Any, path: str, default: Any = None) -> Any:
    """按 "data.output.model" 这样的路径取值，取不到返回 default。"""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part, default)
        elif isinstance(node, list) and part.isdigit():
            index = int(part)
            node = node[index] if index < len(node) else default
        else:
            return default
        if node is default:
            return default
    return node


async def poll_task(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    status_path: str = "status",
    interval: float = 5.0,
    timeout: float = 900.0,
    on_tick: Any = None,
) -> dict[str, Any]:
    """轮询直到任务结束。超时或失败抛 ProviderError（消息写人话，会透给前端）。"""
    started = time.monotonic()
    attempt = 0
    while True:
        response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            raise ProviderError(
                f"查询生成任务失败（HTTP {response.status_code}）：{response.text[:200]}"
            )
        payload = response.json()
        status = str(dig(payload, status_path, "") or "").lower()
        attempt += 1
        if on_tick:
            on_tick(attempt, status)

        if status in DONE_STATES:
            return payload
        if status in FAIL_STATES:
            message = dig(payload, "task_error.message") or dig(payload, "message") or status
            raise ProviderError(f"生成任务失败：{message}")
        if time.monotonic() - started > timeout:
            raise ProviderError(f"生成超时（已等待 {int(timeout)} 秒），请重试或更换 Provider。")
        await asyncio.sleep(interval)


async def download(client: httpx.AsyncClient, url: str, target) -> None:
    """下载产物到本地路径。"""
    response = await client.get(url, follow_redirects=True)
    if response.status_code >= 400:
        raise ProviderError(f"下载生成结果失败（HTTP {response.status_code}）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)


__all__ = ["DONE_STATES", "FAIL_STATES", "dig", "download", "poll_task"]

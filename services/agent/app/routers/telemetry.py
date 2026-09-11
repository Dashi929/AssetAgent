"""埋点的读取与导出接口。

数据全部在本地 `data_dir/telemetry.jsonl`，默认不上传；这里只提供
聚合视图（Settings 页展示）和手动导出（alpha 期用户上报用）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .. import telemetry

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])


@router.get("/summary")
async def get_summary(month: str | None = None) -> dict:
    """埋点聚合：任务量 / 成功率 / 耗时 / 生成与采纳 / 导出次数。"""
    return telemetry.summary(month=month)


@router.get("/export")
async def export_events(month: str | None = None) -> PlainTextResponse:
    """原始埋点导出（NDJSON，每行一个事件）。用户手动保存/上报。"""
    lines = [json.dumps(e, ensure_ascii=False) for e in telemetry.read_events(month=month)]
    body = "\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(body, media_type="application/x-ndjson")

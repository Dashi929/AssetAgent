"""任务查询与取消。

前端轮询这个接口拿进度。任务状态同时落盘（jobs/<id>.json），
所以刷新页面、甚至重启应用之后进度都还在。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import store
from ..jobs import runner

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")
    payload = job.model_dump(mode="json")
    payload["running"] = runner.is_running(job_id)
    return payload


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = store.find_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{job_id}")
    cancelled = runner.cancel(job_id)
    return {"cancelled": cancelled, "job_id": job_id}


@router.get("")
async def list_jobs(asset_id: str) -> list[dict]:
    return [job.model_dump(mode="json") for job in store.list_jobs(asset_id)]

"""任务查询与取消。

前端轮询这个接口拿进度。任务状态同时落盘（jobs/<id>.json），
所以刷新页面、甚至重启应用之后进度都还在。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import store
from ..jobs import runner

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/queue")
async def job_queue() -> dict:
    """任务队列浮窗的数据源：进行中的在前 + 最近完成的若干条，带资产名。

    注意路由顺序：必须注册在 /{job_id} 之前，否则 "queue" 会被当成 job_id。
    """
    items: list[dict] = []
    for job in runner.recent_jobs():
        try:
            asset_name = store.get_asset(job.asset_id).name
        except Exception:  # 资产可能已被删除；任务行仍要能显示
            asset_name = "（已删除的资产）"
        payload = job.model_dump(mode="json")
        payload["asset_name"] = asset_name
        payload["running"] = runner.is_running(job.id)
        items.append(payload)
    return {"jobs": items}


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

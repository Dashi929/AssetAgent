"""任务队列与进度回传。

设计：sidecar 是单机单用户进程，不需要 Celery 那套。用 asyncio 任务 + 落盘状态就够了，
关键是**每一步都写盘**，这样前端刷新/重启后仍能恢复进度，失败也有据可查。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from . import store, telemetry
from .models import Job, JobStatus, JobStep, now

logger = logging.getLogger("assetagent.jobs")

Work = Callable[["ProgressReporter"], Awaitable[None]]


class ProgressReporter:
    """交给管线步骤的进度回调。步骤只管调用，落盘由这里负责。"""

    def __init__(self, job: Job) -> None:
        self.job = job

    def __call__(self, progress: float, message: str = "") -> None:
        self.job.progress = max(0.0, min(1.0, float(progress)))
        if message:
            self.job.message = message
        store.save_job(self.job)

    def stage(self, index: int, total: int, message: str) -> None:
        """按"第几步 / 共几步"上报，管线里用着方便。"""
        total = max(1, total)
        self((index + 1) / total, message)


class JobRunner:
    # 任务队列浮窗只关心"当前"：保留最近这批就够，避免长会话内存无限涨
    MAX_RECENT = 30

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._jobs: dict[str, Job] = {}

    async def submit(self, asset_id: str, step: JobStep, work: Work) -> Job:
        job = store.save_job(Job(asset_id=asset_id, step=step))
        reporter = ProgressReporter(job)
        self._jobs[job.id] = job

        async def _run() -> None:
            job.status = JobStatus.RUNNING
            job.started_at = now()
            store.save_job(job)
            telemetry.record("job_started", asset_id=asset_id, job_id=job.id, step=job.step.value)
            started = time.monotonic()
            failure: BaseException | None = None
            try:
                await work(reporter)
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                job.error = "任务已取消"
                failure = asyncio.CancelledError()
                raise
            except Exception as exc:  # 任务失败必须被记录，不能静默吞掉
                logger.exception("任务失败 job=%s step=%s", job.id, job.step)
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                failure = exc
            else:
                job.status = JobStatus.SUCCEEDED
                job.progress = 1.0
                if not job.message:
                    job.message = "完成"
            finally:
                job.finished_at = now()
                store.save_job(job)
                telemetry.record(
                    "job_finished",
                    asset_id=asset_id,
                    job_id=job.id,
                    step=job.step.value,
                    status=job.status.value,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_class=telemetry.error_class(failure),
                    error=None if failure is None else str(failure)[:500],
                )
                self._tasks.pop(job.id, None)
                self._trim_recent()

        self._tasks[job.id] = asyncio.create_task(_run())
        return job

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def recent_jobs(self) -> list[Job]:
        """进行中的在前（新提交的靠上），已完成的按结束时间倒序排在后面。"""
        running = [j for j in self._jobs.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)]
        finished = [j for j in self._jobs.values() if j.status not in (JobStatus.QUEUED, JobStatus.RUNNING)]
        finished.sort(key=lambda j: j.finished_at or j.created_at, reverse=True)
        return running + finished

    def _trim_recent(self) -> None:
        if len(self._jobs) <= self.MAX_RECENT:
            return
        keep = self.recent_jobs()[: self.MAX_RECENT]
        self._jobs = {job.id: job for job in keep}

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())


runner = JobRunner()

__all__ = ["JobRunner", "ProgressReporter", "runner"]

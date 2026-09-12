"""HTTP 接口层。"""

from . import ai_assist, assets, files, jobs, meta, settings, telemetry

ROUTERS = [
    assets.router,
    ai_assist.router,
    jobs.router,
    settings.router,
    meta.router,
    files.router,
    telemetry.router,
]

__all__ = ["ROUTERS", "ai_assist", "assets", "files", "jobs", "meta", "settings", "telemetry"]

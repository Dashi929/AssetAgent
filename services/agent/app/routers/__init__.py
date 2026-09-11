"""HTTP 接口层。"""

from . import assets, files, jobs, meta, settings, telemetry

ROUTERS = [
    assets.router,
    jobs.router,
    settings.router,
    meta.router,
    files.router,
    telemetry.router,
]

__all__ = ["ROUTERS", "assets", "files", "jobs", "meta", "settings", "telemetry"]

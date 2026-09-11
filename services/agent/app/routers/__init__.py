"""HTTP 接口层。"""

from . import assets, files, jobs, meta, settings

ROUTERS = [assets.router, jobs.router, settings.router, meta.router, files.router]

__all__ = ["ROUTERS", "assets", "files", "jobs", "meta", "settings"]

"""AssetAgent sidecar 服务入口。

只监听 127.0.0.1 —— 这是个本地服务，不是要给局域网用的。
CORS 也只放行开发服务器和 Electron 壳，避免任何网页都能调本地接口。
"""

from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import get_settings
from .jobs import runner
from .providers import ProviderError, registry
from .routers import ROUTERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("assetagent")

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8756",
    "app://assetagent",
    "null",  # Electron 用 file:// 加载渲染进程时 Origin 是 null
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("Blender：%s", settings.blender_bin() or "未检测到（烘焙与 FBX 导出会降级）")
    logger.info("生成路由：%s", settings.effective("route_mode", "byok"))
    configured = registry.configured_names()
    logger.info("已配置 Key 的 Provider：%s", "、".join(configured) or "无（将使用离线占位模式）")
    yield
    await runner.shutdown()
    logger.info("sidecar 已停止")


app = FastAPI(
    title="AssetAgent sidecar",
    description="本地 3D 后处理管线、Game-ready 校验器、生成引擎 Provider 抽象层",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in ROUTERS:
    app.include_router(_router)


# ------------------------------------------------------------------ 全局异常处理

@app.exception_handler(ProviderError)
async def _provider_error_handler(_request: Request, exc: ProviderError) -> JSONResponse:
    """Provider 层的业务错误：消息是写给人看的，直接透给前端。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    """参数或数据错误：给前端一个明确的操作提示。"""
    return JSONResponse(status_code=400, content={"detail": f"参数错误：{exc}"})


@app.exception_handler(Exception)
async def _catchall_handler(_request: Request, exc: Exception) -> JSONResponse:
    """未预期的内部错误：绝不能暴露堆栈给前端，但要留日志方便排查。"""
    logger.error("未处理异常：%s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={
            "detail": "内部错误，请稍后重试。如果反复出现，请在「设置 → 查看日志」中找到详细报错信息并反馈。"
        },
    )


@app.get("/")
async def index() -> dict:
    return {
        "name": "AssetAgent sidecar",
        "version": __version__,
        "docs": "/docs",
        "health": "/api/health",
    }


def run() -> None:
    """`python -m app.main` 或 `assetagent-sidecar` 的入口。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.assetagent_host,
        port=settings.assetagent_port,
        reload=False,
    )


if __name__ == "__main__":
    run()

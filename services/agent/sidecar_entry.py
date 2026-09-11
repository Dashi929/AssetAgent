"""PyInstaller 打包入口。

为什么不用 app.main:app 的字符串引用：
PyInstaller 的静态分析能追踪直接 import，但追踪不了字符串形式的模块引用。
这里直接创建 app 实例并启动 uvicorn，确保所有路由、模型、工具都被打包进去。

注意：本文件必须和 app/ 包同级（services/agent/ 下）。
放在 scripts/ 时 PyInstaller 在入口目录找不到 app 包，会**静默**忽略整个包，
产出一个能启动但一调用就 ModuleNotFoundError 的空壳 exe —— 体积异常小是唯一信号。
"""

from __future__ import annotations

import logging
import os
import sys

# PyInstaller 把资源解压到 _MEIPASS，运行时必须把它加进模块搜索路径。
# 这一步必须在任何 app.* 导入之前完成。
if getattr(sys, "frozen", False):
    _MEIPASS = getattr(sys, "_MEIPASS", None)
    if _MEIPASS and str(_MEIPASS) not in sys.path:
        sys.path.insert(0, str(_MEIPASS))

# 注意：这里不再强制设置 ASSETAGENT_DATA_DIR。
# 打包后数据目录由 app.paths.default_data_dir() 落到系统应用数据目录
# （Windows 即 %LOCALAPPDATA%\AssetAgent），避免卸载时把用户资产一起删掉。

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

import uvicorn  # noqa: E402

# 直接 import app 包触发所有模块的加载，让 PyInstaller 分析器能追踪到
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

logger = logging.getLogger("assetagent.sidecar")


def main() -> None:
    settings = get_settings()
    host = os.environ.get("ASSETAGENT_HOST", settings.assetagent_host)
    port = int(os.environ.get("ASSETAGENT_PORT", settings.assetagent_port))

    # 启动即打印关键路径，真机排障时第一眼就能看出路径有没有解析错
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("规则目录：%s", settings.recipes_dir)
    if not settings.recipes_dir.is_dir():
        logger.warning("规则目录不存在，校验规则与预设将加载失败，将回退到内置默认值")
    logger.info("监听地址：http://%s:%s", host, port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()

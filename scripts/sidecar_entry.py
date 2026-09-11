"""PyInstaller 打包入口。

为什么不用 app.main:app 的字符串引用：
PyInstaller 的静态分析能追踪直接 import，但追踪不了字符串形式的模块引用。
这里直接创建 app 实例并启动 uvicorn，确保所有路由、模型、工具都被打包进去。
"""

from __future__ import annotations

import logging
import os
import sys

# PyInstaller 把资源放在 _MEIPASS；开发时就是当前目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    # 让 app 能找到它自己（app 包在 _MEIPASS 下）
    if os.path.join(sys._MEIPASS, "app") not in sys.path:  # type: ignore[attr-defined]
        sys.path.insert(0, sys._MEIPASS)  # type: ignore[attr-defined]
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据目录默认放在 exe 同级，方便用户找到
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DEFAULT_DATA_DIR, exist_ok=True)

os.environ.setdefault("ASSETAGENT_DATA_DIR", DEFAULT_DATA_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

import uvicorn  # noqa: E402

# 直接 import app 包触发所有模块的加载，让 PyInstaller 分析器能追踪到
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


def main() -> None:
    settings = get_settings()
    host = os.environ.get("ASSETAGENT_HOST", settings.assetagent_host)
    port = int(os.environ.get("ASSETAGENT_PORT", settings.assetagent_port))

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()

"""知识库加载 —— AI 助手的领域知识（提示词优化口径等）。

知识库是**给技术美术编辑的配置**，放 recipes/knowledge/，走 recipes_dir
三级回落（安装目录 > _MEIPASS > 仓库），改文件即时生效。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .config import get_settings

KNOWLEDGE_DIR = "knowledge"


def knowledge_dir() -> Path:
    return get_settings().recipes_dir / KNOWLEDGE_DIR


@lru_cache(maxsize=8)
def _load_cached(resolved: str, mtime_ns: int) -> str:
    return Path(resolved).read_text(encoding="utf-8")


def load_knowledge(name: str, default: str = "") -> str:
    """读一篇知识库文档（markdown）。文件缺失返回 default，不让 AI 调用失败。"""
    path = knowledge_dir() / name
    if not path.exists():
        return default
    resolved = str(path.resolve())
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return default
    return _load_cached(resolved, mtime)


def prompt_optimization_kb() -> str:
    return load_knowledge(
        "prompt_optimization.md",
        default="你是游戏 3D 资产生成专家。优化用户的描述：单一主体、材质风格具体、"
        "比例锚点清晰、避免文字与透明材质。",
    )


__all__ = ["knowledge_dir", "load_knowledge", "prompt_optimization_kb"]

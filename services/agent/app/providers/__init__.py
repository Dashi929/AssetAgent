"""生成引擎 Provider 层。

对外只暴露抽象接口与注册表，上层不该 import 具体某家 Provider。
"""

from .base import (
    Gen3DProvider,
    GenerateRequest,
    ProviderError,
    ProviderNotConfigured,
    VariantResult,
    image_to_data_uri,
)
from .registry import PRIORITY, ProviderRegistry, registry

__all__ = [
    "PRIORITY",
    "Gen3DProvider",
    "GenerateRequest",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRegistry",
    "VariantResult",
    "image_to_data_uri",
    "registry",
]

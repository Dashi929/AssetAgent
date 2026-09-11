"""Provider 注册表与双路由解析。

路由规则（产品策划文档 7.8）：
    已登录且已订阅 → 官方中转（relay，Phase 2）
    未登录或未订阅 → BYOK（用户自填 Key）

MVP 只落地这个开关本身，relay 服务端在 Phase 2。所以 relay 分支现在会明确报错，
而不是静默降级 —— 静默降级会让"为什么没走中转"这种问题极难排查。
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..models import ProviderInfo
from .base import Gen3DProvider, ProviderError
from .local_trellis import LocalTrellisProvider
from .meshy import MeshyProvider
from .mock import MockProvider
from .rodin import RodinProvider
from .tripo import TripoProvider

# 按优先级排列：谁先有 Key 就用谁
PRIORITY = ("meshy", "tripo", "rodin")


class ProviderRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        # 只在显式传入时才固定 Settings（测试用）；否则每次现取
        self._explicit_settings = settings
        self._providers: dict[str, Gen3DProvider] = {}
        self.reload()

    @property
    def settings(self) -> Settings:
        """每次访问都取当前 Settings。

        曾经把 Settings 缓存在实例上，结果 `reset_settings_cache()` 之后 registry
        还在读旧实例 —— 用户填了 API Key，`has_key` 却仍是 False，界面显示"未配置"，
        生成一直走占位模式。这种"配置改了但不生效"是最难排查的一类 bug，
        所以这里刻意不做缓存。
        """
        return self._explicit_settings or get_settings()

    def _build(self) -> dict[str, Gen3DProvider]:
        settings = self.settings
        mode = str(settings.effective("route_mode", "byok"))
        providers = [
            MeshyProvider(settings.provider_key("meshy"), settings.meshy_base_url, mode),
            TripoProvider(settings.provider_key("tripo"), settings.tripo_base_url, mode),
            RodinProvider(settings.provider_key("rodin"), settings.rodin_base_url, mode),
            LocalTrellisProvider(mode=mode),
            MockProvider(mode="mock"),
        ]
        return {p.name: p for p in providers}

    def reload(self) -> None:
        self._providers = self._build()

    # ---- 查询 ----
    def get(self, name: str) -> Gen3DProvider:
        provider = self._providers.get(name)
        if provider is None:
            known = "、".join(sorted(self._providers))
            raise ProviderError(f"未知的生成引擎「{name}」。可用：{known}")
        return provider

    def list_info(self) -> list[ProviderInfo]:
        infos = [p.info() for p in self._providers.values()]
        for info in infos:
            info.cost_per_generation = self.settings.cost_per_generation
        return sorted(infos, key=lambda i: (i.name == "mock", i.name))

    def configured_names(self) -> list[str]:
        return [name for name in PRIORITY if self._providers[name].has_key]

    # ---- 路由 ----
    def resolve(self, preferred: str | None = None) -> Gen3DProvider:
        mode = str(self.settings.effective("route_mode", "byok"))

        if mode == "relay":
            raise ProviderError(
                "官方中转服务将在 Phase 2 上线。当前请先在「设置 → BYOK」中填写自有 API Key 使用。"
            )

        if preferred:
            provider = self.get(preferred)
            if provider.name == "mock":
                return provider
            if not provider.has_key:
                raise ProviderError(
                    f"「{provider.display_name}」还没有配置 API Key，请在设置页填写后再试。"
                )
            return provider

        for name in PRIORITY:
            provider = self._providers[name]
            if provider.has_key:
                return provider

        if bool(self.settings.effective("allow_mock_fallback", True)):
            return self._providers["mock"]

        raise ProviderError(
            "尚未配置任何生成引擎的 API Key。请到「设置 → BYOK」填写 Meshy / Tripo / Rodin 任意一家的 Key；"
            "如果只是想先跑通后处理链路，可以打开「允许离线占位模式」。"
        )

    def estimate_cost(self, num_variants: int, provider_name: str | None = None) -> float:
        """生成前预估本次花费。

        走**已解析的 Provider** 自己的算法，而不是全局单价 × 次数：
        否则离线占位模式会给用户报一个 ¥5 的假价格，本地模型同理。
        """
        provider = self.resolve(provider_name)
        return provider.estimate_cost(num_variants, self.settings.cost_per_generation)


registry = ProviderRegistry()

__all__ = ["PRIORITY", "ProviderRegistry", "registry"]

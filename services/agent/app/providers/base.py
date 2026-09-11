"""生成引擎抽象接口。

上层 Agent / 工具层只认这个接口，不认具体是哪家 Provider —— 这是"生成引擎可替换"的落点。
双路由（BYOK / 官方中转）由 registry 决定，Provider 自己不需要关心。
"""

from __future__ import annotations

import base64
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ProviderInfo, SpecPreset


@dataclass(slots=True)
class GenerateRequest:
    asset_id: str
    out_dir: Path
    image_paths: list[Path] = field(default_factory=list)
    prompt: str = ""
    spec: SpecPreset | None = None
    num_variants: int = 3
    # 生成耗时长，必须能把进度透出去，否则前端只能干等
    on_progress: Callable[[float, str], None] | None = None

    def report(self, progress: float, message: str) -> None:
        if self.on_progress:
            self.on_progress(max(0.0, min(1.0, progress)), message)


@dataclass(slots=True)
class VariantResult:
    provider: str
    mesh_path: Path
    params: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def image_to_data_uri(path: Path) -> str:
    """多数 Provider 接受 base64 data URI，省掉一步图床。"""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


class ProviderError(RuntimeError):
    """Provider 层的可预期错误。消息会直接透给前端，所以要写人话。"""


class ProviderNotConfigured(ProviderError):
    pass


class Gen3DProvider(ABC):
    name: str = "base"
    display_name: str = "Base"
    capabilities: tuple[str, ...] = ("image_to_3d",)
    note: str = ""

    def __init__(self, api_key: str = "", base_url: str = "", mode: str = "byok") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.mode = mode

    # ---- 状态 ----
    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @property
    def available(self) -> bool:
        return self.has_key

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name=self.name,
            display_name=self.display_name,
            mode="byok" if self.mode == "byok" else "relay",  # type: ignore[arg-type]
            available=self.available,
            has_key=self.has_key,
            cost_per_generation=0.0,
            capabilities=list(self.capabilities),
            note=self.note,
        )

    # ---- 能力 ----
    @abstractmethod
    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        """生成 num_variants 个变体。失败必须抛 ProviderError，不要返回空列表。"""

    def estimate_cost(self, req: GenerateRequest, unit_cost: float) -> float:
        """默认按"次数 × 单价"估。有特殊计费的 Provider 覆盖它。"""
        return round(unit_cost * max(1, req.num_variants), 4)

    async def healthcheck(self) -> bool:
        """连通性自检 —— 设置页那个"测试连接"按钮调的就是它。"""
        return self.available

    # ---- 工具 ----
    def _require_key(self) -> str:
        if not self.api_key:
            raise ProviderNotConfigured(
                f"{self.display_name} 尚未配置 API Key。请在「设置 → BYOK」中填写，"
                f"或切换到其它已配置的 Provider。"
            )
        return self.api_key

    @staticmethod
    def _spec_dict(req: GenerateRequest) -> dict[str, Any]:
        spec = req.spec or SpecPreset()
        return {
            "face_budget": spec.face_budget,
            "target_engine": spec.target_engine,
            "texture_resolution": spec.texture_resolution,
            "want_quads": spec.want_quads,
        }


__all__ = [
    "Gen3DProvider",
    "GenerateRequest",
    "ProviderError",
    "ProviderNotConfigured",
    "VariantResult",
    "image_to_data_uri",
]

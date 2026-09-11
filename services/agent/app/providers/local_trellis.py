"""本地生成模型 Provider（Phase 2 占位）。

技术选型：TRELLIS（MIT 许可）优先，Hunyuan3D-2.1 备选（社区许可有附加条款）。
不进 MVP —— 原因是本地推理对显存、CUDA 版本、模型权重的依赖会显著拉长 MVP 周期，
而 MVP 的验证目标是"管线和校验是否可信"，这件事云端 Provider 就能验。

这个类先立在这里，是为了让 registry 的结构不用在 Phase 2 重写。
"""

from __future__ import annotations

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult

PHASE2_MESSAGE = (
    "本地生成模型（TRELLIS）计划在 Phase 2 落地，MVP 阶段请使用云端 Provider 或在设置里配置 BYOK。"
)


class LocalTrellisProvider(Gen3DProvider):
    name = "local_trellis"
    display_name = "本地 TRELLIS"
    capabilities = ("image_to_3d", "offline", "privacy")
    note = "Phase 2。适合有 24GB 级显卡、且要求素材不出机房的工作室。"

    @property
    def available(self) -> bool:
        return False

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        raise ProviderError(PHASE2_MESSAGE)

    def estimate_cost(self, num_variants: int, unit_cost: float) -> float:
        return 0.0  # 本地推理无边际成本，只有电费

    async def healthcheck(self) -> bool:
        return False


__all__ = ["LocalTrellisProvider"]

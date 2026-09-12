"""冻结的数据模型与资产状态机。

对应产品策划文档 5.4.4 —— 这两个接口一旦定下就不该再改，
否则前后端一定返工。改这里之前先改文档。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AssetStatus(str, Enum):
    """资产状态机。

    草稿 → 生成中 → 待挑选 → 后处理中 → 待校验 → 已校验 → 已导出
                          ↘ 失败（可重试）        ↘ 已归档
    """

    DRAFT = "draft"
    GENERATING = "generating"
    AWAITING_PICK = "awaiting_pick"
    PROCESSING = "processing"
    AWAITING_VALIDATION = "awaiting_validation"
    VALIDATED = "validated"
    EXPORTED = "exported"
    FAILED = "failed"
    ARCHIVED = "archived"


class AssetSource(str, Enum):
    IMAGE = "image"
    TEXT = "text"
    MESH = "mesh"


class VersionOp(str, Enum):
    IMPORT = "import"
    GENERATE = "generate"
    REPAIR = "repair"
    DECIMATE = "decimate"
    UV = "uv"
    BAKE = "bake"
    EXPORT = "export"
    # 回滚不删历史：把旧版本的网格复制成新节点（见 routers/assets.py rollback）
    ROLLBACK = "rollback"


class JobStep(str, Enum):
    GENERATE = "generate"
    REPAIR = "repair"
    DECIMATE = "decimate"
    UV = "uv"
    BAKE = "bake"
    VALIDATE = "validate"
    EXPORT = "export"
    RENDER = "render"
    PIPELINE = "pipeline"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIPPED = "skipped"


TargetEngine = Literal["unity", "unreal", "generic"]
LocatorKind = Literal["faces", "uv_islands", "none"]
RouteMode = Literal["byok", "relay"]


class SpecPreset(BaseModel):
    """资产规格预设。前端"选规格"选的就是这个。"""

    name: str = "默认道具"
    category: str = "prop"
    face_budget: int = 5000
    want_quads: bool = True
    target_engine: TargetEngine = "unity"
    unit_scale: float = 1.0  # 1.0 = 米制
    expected_size_m: float | None = None  # 期望最长边尺寸（米），用于单位校验
    texture_resolution: int = 2048
    pivot: Literal["bottom_center", "origin", "custom"] = "bottom_center"
    naming_pattern: str = r"^SM_[A-Za-z0-9_]+$"


class Asset(BaseModel):
    id: str = Field(default_factory=lambda: new_id("asset"))
    name: str = "未命名资产"
    style_id: str | None = None
    source: AssetSource = AssetSource.IMAGE
    status: AssetStatus = AssetStatus.DRAFT
    spec: SpecPreset = Field(default_factory=SpecPreset)
    source_files: list[str] = Field(default_factory=list)  # 只读
    picked_variant_id: str | None = None
    head_version_id: str | None = None
    prompt: str = ""
    # AI 助手按知识库优化过的描述（generate 时优先级低于用户手输的 prompt）
    enhanced_prompt: str = ""
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Variant(BaseModel):
    id: str = Field(default_factory=lambda: new_id("var"))
    asset_id: str
    provider: str
    params: dict[str, Any] = Field(default_factory=dict)
    mesh_path: str
    thumbnail_path: str | None = None
    turntable_paths: list[str] = Field(default_factory=list)
    cost: float = 0.0
    face_count: int | None = None
    created_at: datetime = Field(default_factory=now)


class VersionNode(BaseModel):
    """版本树节点。变体也是节点（op=generate），所以对比与回滚共用一套机制。"""

    id: str = Field(default_factory=lambda: new_id("ver"))
    asset_id: str
    parent_id: str | None = None
    op: VersionOp
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    mesh_path: str
    stats: dict[str, Any] = Field(default_factory=dict)
    skipped_reason: str | None = None
    created_at: datetime = Field(default_factory=now)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: new_id("job"))
    asset_id: str
    step: JobStep
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    message: str = ""
    cost: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Locator(BaseModel):
    """校验失败项的定位信息 —— 前端"点击 FAIL → 高亮问题面/UV 岛"靠它。"""

    kind: LocatorKind = "none"
    indices: list[int] = Field(default_factory=list)
    uv_bbox: list[float] | None = None  # [u_min, v_min, u_max, v_max]


class RuleResult(BaseModel):
    rule: str
    label: str
    result: CheckResult
    value: Any = None
    threshold: Any = None
    message: str = ""
    locator: Locator = Field(default_factory=Locator)


class ValidationReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rpt"))
    asset_id: str
    version_id: str
    ruleset: str = "default"
    passed: bool = False
    results: list[RuleResult] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if r.result == CheckResult.FAIL]

    def recompute(self) -> ValidationReport:
        self.passed = not self.failures
        return self


class SemanticIssue(BaseModel):
    severity: Literal["high", "medium", "low"] = "medium"
    message: str


class SemanticCheck(BaseModel):
    """视觉 LLM 的语义校验结论：生成结果 vs 概念图/描述。"""

    id: str = Field(default_factory=lambda: new_id("chk"))
    asset_id: str
    version_id: str | None = None
    passed: bool = False
    score: int = 0  # 0-100：与描述/概念图的整体吻合度
    summary: str = ""
    issues: list[SemanticIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


class ExportRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    asset_id: str
    version_id: str
    preset: str
    files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)


class ProviderInfo(BaseModel):
    name: str
    display_name: str
    mode: Literal["byok", "relay", "mock"] = "byok"
    available: bool = False
    has_key: bool = False
    cost_per_generation: float = 0.0
    capabilities: list[str] = Field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------- 请求体


class CreateAssetBody(BaseModel):
    name: str = "未命名资产"
    source: AssetSource = AssetSource.IMAGE
    spec: SpecPreset = Field(default_factory=SpecPreset)
    prompt: str = ""
    tags: list[str] = Field(default_factory=list)


class GenerateBody(BaseModel):
    num_variants: int = 3
    provider: str | None = None
    prompt: str = ""


class PickVariantBody(BaseModel):
    variant_id: str


class RollbackBody(BaseModel):
    version_id: str


class PipelineBody(BaseModel):
    steps: list[JobStep] | None = None  # 默认跑 repair/decimate/uv/bake/validate


class ExportBody(BaseModel):
    preset: str = "unity"
    validate_first: bool = True
    allow_failed_export: bool = False


class SettingsBody(BaseModel):
    route_mode: RouteMode | None = None
    monthly_budget_cny: float | None = None
    cost_per_generation_cny: float | None = None
    default_face_budget: int | None = None
    blender_bin: str | None = None
    allow_mock_fallback: bool | None = None


class ByokBody(BaseModel):
    provider: str
    key: str = ""


# 校验通过后自动推进的状态；FAIL 停在 awaiting_validation，由美术决定修复/忽略/重生成
STATUS_AFTER_VALIDATION = {
    True: AssetStatus.VALIDATED,
    False: AssetStatus.AWAITING_VALIDATION,
}

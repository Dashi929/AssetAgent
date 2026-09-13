"""Game-ready 校验器。

这是"程序通道"的 Critic —— 规则引擎，不是模型。存在的意义是把"能不能进引擎"这件事
从美术的经验判断变成可复现、可追溯、可定位的客观结论。

每条规则都必须能给出 **locator**：只说"UV 有重叠"没用，得说出是哪几个面，
前端才能把问题高亮到视口上。给不出定位的规则等于没有。

规则配置在 recipes/validation_rules.yaml，三级回落：类目配置 > defaults > 代码内置默认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
import yaml

from ..config import get_settings
from ..models import CheckResult, Locator, RuleResult, SpecPreset, ValidationReport, new_id
from .mesh_io import bounds, mesh_stats, ngon_faces_from_file, quad_ratio_from_file
from .uv import uv_island_margin_px, uv_overlap

DEFAULT_RULES: dict[str, Any] = {
    "face_budget": {"enabled": True, "severity": "fail", "max": 5000},
    "quad_ratio": {"enabled": True, "severity": "fail", "min": 0.85},
    "ngon": {"enabled": True, "severity": "fail", "max": 0},
    "uv_overlap": {"enabled": True, "severity": "fail", "max_faces": 0},
    "uv_island_margin": {
        "enabled": True,
        "severity": "fail",
        "min_px": 2.0,
        "reference_resolution": 4096,
    },
    "pivot": {"enabled": True, "severity": "fail", "mode": "bottom_center", "tolerance_m": 0.02},
    "units": {"enabled": True, "severity": "fail", "unit": "meter", "tolerance_ratio": 0.05},
    "naming": {"enabled": True, "severity": "fail", "pattern": r"^SM_[A-Za-z0-9_]+$"},
    "texture": {"enabled": True, "severity": "warn", "power_of_two": True},
}

RULE_LABELS = {
    "face_budget": "面数预算",
    "quad_ratio": "四边面比例",
    "ngon": "N-gon",
    "uv_overlap": "UV 重叠",
    "uv_island_margin": "UV 岛间距",
    "pivot": "轴心",
    "units": "真实单位",
    "naming": "命名规范",
    "texture": "贴图规格",
}

RULE_ORDER = list(DEFAULT_RULES.keys())


@dataclass
class ValidationContext:
    mesh: trimesh.Trimesh
    mesh_path: Path
    spec: SpecPreset
    category: str = "prop"
    asset_name: str = ""
    texture_files: list[Path] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 配置


def load_rules(category: str = "prop") -> dict[str, Any]:
    """三级回落合并：内置默认 < defaults < categories[category]。"""
    merged: dict[str, Any] = {k: dict(v) for k, v in DEFAULT_RULES.items()}
    config_path = get_settings().recipes_dir / "validation_rules.yaml"
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        for key, value in (loaded.get("defaults") or {}).items():
            if isinstance(value, dict):
                merged.setdefault(key, {}).update(value)
            else:
                merged.setdefault(key, {})["max"] = value
        category_overrides = (loaded.get("categories") or {}).get(category) or {}
        for key, value in category_overrides.items():
            if isinstance(value, dict):
                merged.setdefault(key, {}).update(value)
    return merged


# ------------------------------------------------------------------ 判定辅助


def _verdict(ok: bool, rule_config: dict[str, Any]) -> CheckResult:
    if ok:
        return CheckResult.PASS
    return CheckResult.WARN if str(rule_config.get("severity", "fail")) == "warn" else CheckResult.FAIL


def _skipped(rule: str, label: str, reason: str, threshold: Any = None) -> RuleResult:
    """测不了就说测不了。返回一个假的 PASS 比返回 SKIPPED 危险得多。"""
    return RuleResult(
        rule=rule,
        label=label,
        result=CheckResult.SKIPPED,
        threshold=threshold,
        message=reason,
    )


def _disabled(rule: str, label: str, rule_config: dict[str, Any]) -> RuleResult:
    return _skipped(rule, label, "该规则在当前资产类目下已关闭", rule_config)


# ------------------------------------------------------------------ 规则实现


def _rule_face_budget(ctx: ValidationContext) -> RuleResult:
    rule, label = "face_budget", RULE_LABELS["face_budget"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    if ctx.spec.face_budget is None:
        # 导入的资产不限面数：这条契约规则不适用（测不了就说测不了）
        return _skipped(rule, label, "该资产不限面数（导入模型不削减面数）")
    # 优先级：规格预设 > 类目配置 > 全局默认。
    # 规格预设是美术为**这个资产**明确选的交付契约，比工具的类目默认值更具体，
    # 所以它优先。想让类目配置压过预设，应该给该类目单独配一个规格预设。
    budget = int(ctx.spec.face_budget)
    faces = int(len(ctx.mesh.faces))
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(faces <= budget, config),
        value=faces,
        threshold=budget,
        message=f"当前 {faces} 三角面，预算 {budget}",
        locator=Locator(kind="none"),
    )


def _rule_quad_ratio(ctx: ValidationContext) -> RuleResult:
    rule, label = "quad_ratio", RULE_LABELS["quad_ratio"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    minimum = float(config.get("min", 0.85))
    ratio = quad_ratio_from_file(ctx.mesh_path)
    if ratio is None:
        return _skipped(
            rule,
            label,
            f"{ctx.mesh_path.suffix} 无法还原四边面信息（四边面比例只能从 OBJ 原文件统计，"
            f"加载器会把所有面三角化）。如需校验此项，请在管线里保留 OBJ 中间产物。",
            minimum,
        )
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(ratio >= minimum, config),
        value=ratio,
        threshold=minimum,
        message=f"四边面比例 {ratio:.1%}，要求 ≥ {minimum:.0%}",
        locator=Locator(kind="none"),
    )


def _rule_ngon(ctx: ValidationContext) -> RuleResult:
    rule, label = "ngon", RULE_LABELS["ngon"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    maximum = int(config.get("max", 0))
    faces = ngon_faces_from_file(ctx.mesh_path)
    if faces is None:
        return _skipped(
            rule,
            label,
            f"{ctx.mesh_path.suffix} 无法还原 n-gon 信息（仅 OBJ 原文件可统计）。",
            maximum,
        )
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(len(faces) <= maximum, config),
        value=len(faces),
        threshold=maximum,
        message=f"发现 {len(faces)} 个 n-gon，要求 {maximum} 个",
        locator=Locator(kind="faces", indices=faces[:2000]),
    )


def _rule_uv_overlap(ctx: ValidationContext) -> RuleResult:
    rule, label = "uv_overlap", RULE_LABELS["uv_overlap"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    maximum = int(config.get("max_faces", 0))
    report = uv_overlap(ctx.mesh)
    if not report.get("computable"):
        return _skipped(rule, label, str(report.get("reason")), maximum)
    faces = report.get("overlap_faces", [])
    bbox = None
    if faces:
        coords = ctx.mesh.visual.uv[np.asarray(ctx.mesh.faces)[faces]] if faces else None
        if coords is not None:
            flat = np.asarray(coords, dtype=np.float64).reshape(-1, 2)
            bbox = [
                round(float(flat[:, 0].min()), 5),
                round(float(flat[:, 1].min()), 5),
                round(float(flat[:, 0].max()), 5),
                round(float(flat[:, 1].max()), 5),
            ]
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(len(faces) <= maximum, config),
        value=len(faces),
        threshold=maximum,
        message=f"检出 {len(faces)} 个面存在 UV 重叠",
        locator=Locator(kind="uv_islands", indices=faces[:2000], uv_bbox=bbox),
    )


def _rule_uv_island_margin(ctx: ValidationContext) -> RuleResult:
    rule, label = "uv_island_margin", RULE_LABELS["uv_island_margin"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    minimum = float(config.get("min_px", 2.0))
    resolution = int(config.get("reference_resolution", ctx.spec.texture_resolution))
    margin = uv_island_margin_px(ctx.mesh, resolution)
    if margin is None:
        return _skipped(
            rule,
            label,
            "无法计算岛间距（该资产没有 UV，或 UV 岛边界过于复杂超出计算上限）。",
            minimum,
        )
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(margin >= minimum, config),
        value=margin,
        threshold=minimum,
        message=f"最小岛间距 {margin}px（{resolution} 贴图基准），要求 ≥ {minimum}px",
        locator=Locator(kind="none"),
    )


def _rule_pivot(ctx: ValidationContext) -> RuleResult:
    rule, label = "pivot", RULE_LABELS["pivot"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    mode = str(ctx.spec.pivot or config.get("mode", "bottom_center"))
    tolerance = float(config.get("tolerance_m", 0.02))

    if mode != "bottom_center":
        return _skipped(rule, label, f"轴心模式「{mode}」暂未实现校验。", mode)

    # Y-up 约定（与 repair 归一化、GLB/three.js 一致）：底面 = Y 最小值，XZ 居中
    low, high = bounds(ctx.mesh)
    center_xz = (low[[0, 2]] + high[[0, 2]]) / 2.0
    offset = float(np.max(np.abs(np.concatenate([center_xz, [low[1]]]))))
    ok = offset <= tolerance
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(ok, config),
        value=round(offset, 5),
        threshold=tolerance,
        message=(
            f"底面中心偏差 {offset * 100:.2f}cm（X/Z 居中 + 底面贴地），"
            f"容差 ±{tolerance * 100:.2f}cm"
        ),
        locator=Locator(kind="none"),
    )


def _rule_units(ctx: ValidationContext) -> RuleResult:
    rule, label = "units", RULE_LABELS["units"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    expected = ctx.spec.expected_size_m
    if not expected or expected <= 0:
        return _skipped(
            rule,
            label,
            "规格预设里没有填期望尺寸（expected_size_m），无法判断单位是否正确。",
            None,
        )
    tolerance = float(config.get("tolerance_ratio", 0.05))
    low, high = bounds(ctx.mesh)
    actual = float(np.max(high - low))
    deviation = abs(actual - expected) / expected
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(deviation <= tolerance, config),
        value=round(actual, 5),
        threshold=expected,
        message=(
            f"最长边 {actual:.4f}m，期望 {expected:.4f}m，"
            f"偏差 {deviation:.1%}（容差 ±{tolerance:.0%}）"
        ),
        locator=Locator(kind="none"),
    )


def _rule_naming(ctx: ValidationContext) -> RuleResult:
    rule, label = "naming", RULE_LABELS["naming"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    pattern = str(ctx.spec.naming_pattern or config.get("pattern") or r"^SM_[A-Za-z0-9_]+$")
    name = ctx.asset_name or ctx.mesh_path.stem
    try:
        matched = re.match(pattern, name) is not None
    except re.error as exc:
        return _skipped(rule, label, f"命名正则本身写错了：{exc}", pattern)
    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(matched, config),
        value=name,
        threshold=pattern,
        message=f"资产名「{name}」{'符合' if matched else '不符合'}规范 {pattern}",
        locator=Locator(kind="none"),
    )


def _rule_texture(ctx: ValidationContext) -> RuleResult:
    rule, label = "texture", RULE_LABELS["texture"]
    config = ctx.rules.get(rule, {})
    if not config.get("enabled", True):
        return _disabled(rule, label, config)
    if not ctx.texture_files:
        return _skipped(rule, label, "该版本还没有贴图，跳过贴图规格校验。", None)

    problems: list[str] = []
    for path in ctx.texture_files:
        size = _image_size(path)
        if size is None:
            problems.append(f"{path.name} 无法读取尺寸")
            continue
        width, height = size
        if config.get("power_of_two", True) and not (_is_power_of_two(width) and _is_power_of_two(height)):
            problems.append(f"{path.name} 尺寸 {width}×{height} 不是 2 的幂")

    return RuleResult(
        rule=rule,
        label=label,
        result=_verdict(not problems, config),
        value=len(ctx.texture_files),
        threshold="2 的幂",
        message="；".join(problems) if problems else f"{len(ctx.texture_files)} 张贴图规格正常",
        locator=Locator(kind="none"),
    )


def _is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def _image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return None


RULES = {
    "face_budget": _rule_face_budget,
    "quad_ratio": _rule_quad_ratio,
    "ngon": _rule_ngon,
    "uv_overlap": _rule_uv_overlap,
    "uv_island_margin": _rule_uv_island_margin,
    "pivot": _rule_pivot,
    "units": _rule_units,
    "naming": _rule_naming,
    "texture": _rule_texture,
}


# ------------------------------------------------------------------ 入口


def validate(
    mesh: trimesh.Trimesh,
    mesh_path: Path,
    asset_id: str,
    version_id: str,
    spec: SpecPreset,
    *,
    asset_name: str = "",
    category: str = "prop",
    texture_files: list[Path] | None = None,
    ruleset: str = "default",
) -> ValidationReport:
    ctx = ValidationContext(
        mesh=mesh,
        mesh_path=Path(mesh_path),
        spec=spec,
        category=category or spec.category,
        asset_name=asset_name,
        texture_files=texture_files or [],
        rules=load_rules(category or spec.category),
    )

    results: list[RuleResult] = []
    for key in RULE_ORDER:
        rule_fn = RULES.get(key)
        if rule_fn is None:
            continue
        try:
            results.append(rule_fn(ctx))
        except Exception as exc:  # 单条规则崩了不能拖垮整份报告
            results.append(
                RuleResult(
                    rule=key,
                    label=RULE_LABELS.get(key, key),
                    result=CheckResult.SKIPPED,
                    message=f"规则执行异常：{type(exc).__name__}: {exc}",
                )
            )

    report = ValidationReport(
        id=new_id("rpt"),
        asset_id=asset_id,
        version_id=version_id,
        ruleset=ruleset,
        results=results,
        stats=mesh_stats(mesh),
    )
    return report.recompute()


def summarize(report: ValidationReport) -> dict[str, int]:
    counts = {state.value: 0 for state in CheckResult}
    for result in report.results:
        counts[result.result.value] += 1
    return counts


__all__ = [
    "DEFAULT_RULES",
    "RULE_LABELS",
    "RULE_ORDER",
    "RULES",
    "ValidationContext",
    "load_rules",
    "summarize",
    "validate",
]

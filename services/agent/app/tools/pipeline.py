"""管线编排 —— 把各步骤串起来，每一步落一个版本节点。

这是"生成之后的整条交付管线"，也就是产品真正的护城河所在。

编排原则：
1. **每一步都写盘**：中间产物不只在内存里存在，失败可以从任意节点重跑。
2. **每步一个版本节点**：变体对比、版本回滚、失败重跑全部复用同一套机制。
3. **能降级不失败**：烘焙缺 Blender → skip；减面缺后端 → skip。
   一个"少了两张贴图但能进引擎"的资产，比一个失败的任务有价值得多。
4. **失败停在 awaiting_validation**：校验 FAIL 不自动重试到底，交回给美术决定
   "自动修复 / 忽略 / 重生成"（见产品策划文档 5.3）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from .. import store, telemetry
from ..jobs import ProgressReporter
from ..models import (
    AssetStatus,
    JobStep,
    SpecPreset,
    ValidationReport,
    VersionNode,
    VersionOp,
)
from . import bake as bake_tool
from . import render as render_tool
from .decimate import decimate_mesh
from .mesh_io import load_mesh, save_mesh
from .repair import normalize_transform, repair_mesh
from .uv import unwrap
from .validate import summarize, validate

logger = logging.getLogger("assetagent.pipeline")

DEFAULT_STEPS: list[JobStep] = [
    JobStep.REPAIR,
    JobStep.DECIMATE,
    JobStep.UV,
    JobStep.BAKE,
    JobStep.VALIDATE,
]

STEP_OP = {
    JobStep.REPAIR: VersionOp.REPAIR,
    JobStep.DECIMATE: VersionOp.DECIMATE,
    JobStep.UV: VersionOp.UV,
    JobStep.BAKE: VersionOp.BAKE,
}


class PipelineError(RuntimeError):
    pass


def resolve_input(asset_id: str) -> tuple[Path, str | None]:
    """决定管线的输入网格：优先用美术挑中的变体，否则用当前版本树的头节点。"""
    asset = store.get_asset(asset_id)

    if asset.picked_variant_id:
        variant = store.get_variant(asset_id, asset.picked_variant_id)
        return Path(variant.mesh_path), None

    head = store.head_version(asset_id)
    if head is not None:
        return Path(head.mesh_path), head.id

    variants = store.list_variants(asset_id)
    if variants:
        return Path(variants[0].mesh_path), None

    raise PipelineError(
        "这个资产还没有可处理的网格。请先生成变体并挑选一个，或直接把已有网格拖进来（工作流 C）。"
    )


def _new_version(asset_id: str, parent_id: str | None, op: VersionOp, label: str, params: dict) -> VersionNode:
    directory = store.new_artifact_dir(asset_id, "versions", "ver")
    return VersionNode(
        id=directory.name,
        asset_id=asset_id,
        parent_id=parent_id,
        op=op,
        label=label,
        params=params,
        mesh_path=str(directory / "mesh.glb"),
    )


def _write_version_mesh(mesh, node: VersionNode, also_obj: bool = True) -> None:
    """把版本产物落盘。同时留一份 OBJ —— 四边面 / n-gon 这两条规则只能从 OBJ 原文件统计。"""
    directory = store.version_dir(node.asset_id, node.id)
    directory.mkdir(parents=True, exist_ok=True)
    save_mesh(mesh, directory / "mesh.glb")
    node.mesh_path = str(directory / "mesh.glb")
    if also_obj:
        try:
            save_mesh(mesh, directory / "mesh.obj")
            node.stats["obj_path"] = str(directory / "mesh.obj")
        except Exception as exc:  # OBJ 只是为了让规则能测，出不来不影响主流程
            node.stats["obj_error"] = str(exc)


def _rule_measure_path(node: VersionNode) -> Path:
    """规则引擎读哪个文件：优先 OBJ（能还原四边面与 n-gon），退回 GLB。"""
    obj_path = node.stats.get("obj_path")
    if obj_path and Path(obj_path).exists():
        return Path(obj_path)
    return Path(node.mesh_path)


async def run_pipeline(
    asset_id: str,
    progress: ProgressReporter,
    steps: list[JobStep] | None = None,
    spec_override: SpecPreset | None = None,
) -> dict[str, Any]:
    """跑完整条后处理管线。返回执行摘要。"""
    asset = store.get_asset(asset_id)
    spec = spec_override or asset.spec
    step_list = [s for s in (steps or DEFAULT_STEPS)]
    if JobStep.VALIDATE not in step_list:
        step_list.append(JobStep.VALIDATE)

    source_path, parent_id = resolve_input(asset_id)
    total = len(step_list)

    store.patch_asset(asset_id, status=AssetStatus.PROCESSING)
    progress(0.02, f"读取输入网格：{source_path.name}")

    mesh = await asyncio.to_thread(load_mesh, source_path)
    source_high_path = source_path
    summary: dict[str, Any] = {
        "asset_id": asset_id,
        "steps": [],
        "report_id": None,
        "thumbnail": None,
        "textures": [],
    }

    baked_textures: list[Path] = []
    current = mesh

    for index, step in enumerate(step_list):
        progress.stage(index, total, f"执行 {step.value}…")
        step_started = time.monotonic()

        try:
            if step is JobStep.REPAIR:
                current, payload = await _step_repair(current, spec)
            elif step is JobStep.DECIMATE:
                current, payload = await _step_decimate(current, spec)
            elif step is JobStep.UV:
                current, payload = await _step_uv(current)
            elif step is JobStep.BAKE:
                current, payload, baked_textures = await _step_bake(
                    current, source_high_path, asset_id, spec
                )
            elif step is JobStep.VALIDATE:
                current, payload = await _step_validate(
                    current, asset_id, parent_id, spec, asset.name, baked_textures, summary
                )
            else:
                continue
        except Exception as exc:
            # 每步耗时与失败原因都进埋点；异常继续上抛由任务层记 job_finished
            telemetry.record(
                "pipeline_step",
                asset_id=asset_id,
                step=step.value,
                duration_ms=int((time.monotonic() - step_started) * 1000),
                ok=False,
                error_class=telemetry.error_class(exc),
            )
            raise

        # 每一步都产生一个版本节点（validate 除外，它不改网格）
        if step in STEP_OP:
            node = _new_version(
                asset_id, parent_id, STEP_OP[step], f"{step.value}", payload.get("params", {})
            )
            await asyncio.to_thread(_write_version_mesh, current, node)
            node.stats.update(payload.get("stats", {}))
            skipped = payload.get("skipped_reason")
            if skipped:
                node.skipped_reason = str(skipped)
                node.stats["skipped"] = True
            store.add_version(node)
            parent_id = node.id
            payload["version_id"] = node.id
            payload["skipped_reason"] = skipped

        payload["step"] = step.value
        summary["steps"].append(payload)
        telemetry.record(
            "pipeline_step",
            asset_id=asset_id,
            step=step.value,
            duration_ms=int((time.monotonic() - step_started) * 1000),
            ok=True,
        )

    # 缩略图（资产库网格用）
    thumb_dir = store.asset_dir(asset_id) / "thumbnails"
    thumb = await asyncio.to_thread(
        render_tool.render_thumbnail, current, thumb_dir / "thumbnail.png", 256
    )
    summary["thumbnail"] = thumb.get("path")

    progress(1.0, "管线完成")
    return summary


# ------------------------------------------------------------------ 各步骤


async def _step_repair(mesh, spec: SpecPreset) -> tuple[Any, dict[str, Any]]:
    repaired, report = await asyncio.to_thread(repair_mesh, mesh)
    normalized, transform = await asyncio.to_thread(
        normalize_transform, repaired, spec.pivot, spec.expected_size_m
    )
    actions = list(report.get("actions", [])) + list(transform.get("actions", []))
    return normalized, {
        "params": {"pivot": spec.pivot, "expected_size_m": spec.expected_size_m},
        "actions": actions,
        "stats": {"before": report.get("before"), "after": report.get("after")},
    }


async def _step_decimate(mesh, spec: SpecPreset) -> tuple[Any, dict[str, Any]]:
    decimated, report = await asyncio.to_thread(
        decimate_mesh, mesh, spec.face_budget, spec.want_quads
    )
    return decimated, {
        "params": {"target_faces": spec.face_budget, "want_quads": spec.want_quads},
        "method": report.get("method"),
        "before_faces": report.get("before_faces"),
        "after_faces": report.get("after_faces"),
        "skipped_reason": report.get("skipped_reason"),
        "stats": {"after_faces": report.get("after_faces")},
    }


async def _step_uv(mesh) -> tuple[Any, dict[str, Any]]:
    unwrapped, report = await asyncio.to_thread(unwrap, mesh)
    return unwrapped, {
        "params": {"method": report.get("method")},
        "uv_islands": report.get("uv_islands"),
        "skipped_reason": report.get("skipped_reason"),
        "stats": {"uv_islands": report.get("uv_islands")},
    }


async def _step_bake(mesh, high_path: Path, asset_id: str, spec: SpecPreset):
    """烘焙：高模（上游原始网格）→ 低模（当前网格）。

    低模此时还没有版本目录（版本节点是在步骤跑完之后才建的），
    所以先把它落到 work/ 下的临时文件，烘焙脚本需要真实文件路径而不是内存里的网格。
    """
    work_dir = store.asset_dir(asset_id) / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    low_path = store.unique_path(work_dir, "low_for_bake.glb")
    await asyncio.to_thread(save_mesh, mesh, low_path)

    out_dir = store.asset_dir(asset_id) / "textures"
    report = await asyncio.to_thread(
        bake_tool.bake_textures,
        low_path,
        high_path,
        out_dir,
        spec.texture_resolution,
    )
    textures = [Path(p) for p in report.get("files", [])]
    report["low_mesh"] = str(low_path)
    return mesh, report, textures


async def _step_validate(
    mesh,
    asset_id: str,
    parent_id: str | None,
    spec: SpecPreset,
    asset_name: str,
    textures: list[Path],
    summary: dict[str, Any],
):
    """校验不改网格，只产出报告并推进资产状态。"""
    if not parent_id:
        raise PipelineError("管线内部错误：校验阶段没有上游版本可以校验。")

    version_directory = store.version_dir(asset_id, parent_id)
    obj_path = version_directory / "mesh.obj"
    glb_path = version_directory / "mesh.glb"
    measure_path = obj_path if obj_path.exists() else glb_path
    if not measure_path.exists():
        raise PipelineError("管线内部错误：校验阶段找不到上游版本的网格文件。")

    report: ValidationReport = await asyncio.to_thread(
        validate,
        mesh,
        measure_path,
        asset_id,
        parent_id,
        spec,
        asset_name=asset_name,
        category=spec.category,
        texture_files=textures,
    )
    store.save_report(report)
    summary["report_id"] = report.id
    summary["textures"] = [str(p) for p in textures]
    telemetry.record(
        "validation",
        asset_id=asset_id,
        version_id=parent_id,
        passed=report.passed,
        failures=[r.label for r in report.failures],
        counts=summarize(report),
    )

    store.patch_asset(
        asset_id,
        status=AssetStatus.VALIDATED if report.passed else AssetStatus.AWAITING_VALIDATION,
    )

    return mesh, {
        "params": {"ruleset": report.ruleset},
        "report_id": report.id,
        "passed": report.passed,
        "counts": summarize(report),
        "stats": {},
    }


__all__ = ["DEFAULT_STEPS", "PipelineError", "resolve_input", "run_pipeline"]

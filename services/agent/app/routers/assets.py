"""资产相关的 REST 接口。

这一层刻意做得很薄：只做参数校验、状态推进和任务提交，业务逻辑全在 store / tools / providers 里。
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import store, telemetry
from ..config import get_settings
from ..jobs import runner
from ..models import (
    Asset,
    AssetSource,
    AssetStatus,
    CreateAssetBody,
    ExportBody,
    ExportRecord,
    GenerateBody,
    JobStep,
    PickVariantBody,
    PipelineBody,
    RollbackBody,
    SpecPreset,
    Variant,
    VersionNode,
    VersionOp,
)
from ..presets import load_spec_presets
from ..providers import GenerateRequest, ProviderError, registry
from ..tools import (
    MeshError,
    convert_to_glb,
    export_asset,
    load_mesh,
    render_thumbnail,
    run_pipeline,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
# .fbx 也在列：导入时经 Blender 转成 GLB 工作副本（tools/convert.py），
# 管线与视口全程只见 GLB
MESH_SUFFIXES = {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx"}


# ------------------------------------------------------------------ 序列化


def _asset_summary(asset: Asset) -> dict[str, Any]:
    variants = store.list_variants(asset.id)
    versions = store.list_versions(asset.id)
    report = store.latest_report(asset.id)
    exports = store.list_exports(asset.id)
    thumbnail = None
    thumb_path = store.asset_dir(asset.id) / "thumbnails" / "thumbnail.png"
    if thumb_path.exists():
        thumbnail = str(thumb_path)
    elif variants and variants[-1].thumbnail_path:
        thumbnail = variants[-1].thumbnail_path
    turntable = sorted((store.asset_dir(asset.id) / "turntable").glob("turntable_*.png"))

    return {
        "asset": asset.model_dump(mode="json"),
        "counts": {
            "variants": len(variants),
            "versions": len(versions),
            "exports": len(exports),
        },
        "thumbnail": thumbnail,
        "turntable": [str(p) for p in turntable],
        "validation": None
        if report is None
        else {
            "id": report.id,
            "passed": report.passed,
            "created_at": report.created_at.isoformat(),
            "failed_rules": [r.label for r in report.failures],
        },
    }


def _require_asset(asset_id: str) -> Asset:
    try:
        return store.get_asset(asset_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _image_sources(asset: Asset) -> list[Path]:
    return [Path(p) for p in asset.source_files if Path(p).suffix.lower() in IMAGE_SUFFIXES]


# ------------------------------------------------------------------ 列表 / 详情


@router.get("")
async def list_assets(include_archived: bool = False) -> list[dict[str, Any]]:
    return [_asset_summary(asset) for asset in store.list_assets(include_archived=include_archived)]


@router.get("/{asset_id}")
async def get_asset(asset_id: str) -> dict[str, Any]:
    asset = _require_asset(asset_id)
    detail = _asset_summary(asset)
    detail["variants"] = [v.model_dump(mode="json") for v in store.list_variants(asset_id)]
    detail["versions"] = [v.model_dump(mode="json") for v in store.list_versions(asset_id)]
    detail["reports"] = [r.model_dump(mode="json") for r in store.list_reports(asset_id)]
    detail["semantic_checks"] = [c.model_dump(mode="json") for c in store.list_semantic_checks(asset_id)]
    detail["exports"] = [e.model_dump(mode="json") for e in store.list_exports(asset_id)]
    detail["jobs"] = [j.model_dump(mode="json") for j in store.list_jobs(asset_id)]
    return detail


# ------------------------------------------------------------------ 创建


@router.post("", status_code=201)
async def create_asset(body: CreateAssetBody) -> dict[str, Any]:
    asset = store.create_asset(
        name=body.name,
        spec=body.spec,
        source=body.source.value,
        prompt=body.prompt,
        tags=body.tags,
    )
    telemetry.record("create_asset", asset_id=asset.id, source=asset.source)
    return _asset_summary(asset)


@router.post("/upload", status_code=201)
async def create_asset_with_images(
    files: list[UploadFile] = File(...),
    name: str = Form("未命名资产"),
    prompt: str = Form(""),
    preset_key: str = Form(""),
) -> dict[str, Any]:
    """工作流 A 的入口：拖入概念图（可多张，主视图 + 正/侧视图）。"""
    spec = _resolve_preset(preset_key)
    asset = store.create_asset(name=name, spec=spec, source=AssetSource.IMAGE.value, prompt=prompt)

    saved = 0
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 {suffix}。请上传 {'、'.join(sorted(IMAGE_SUFFIXES))} 格式的概念图。",
            )
        data = await upload.read()
        if not data:
            continue
        store.write_source_bytes(asset.id, Path(upload.filename or "concept.png").name, data)
        saved += 1

    if saved == 0:
        store.archive_asset(asset.id)
        raise HTTPException(status_code=400, detail="没有读到任何有效的图片内容。")

    telemetry.record("create_asset", asset_id=asset.id, source=asset.source, images=saved)
    return _asset_summary(store.get_asset(asset.id))


@router.post("/import-mesh", status_code=201)
async def create_asset_from_mesh(
    file: UploadFile = File(...),
    name: str = Form("未命名资产"),
    preset_key: str = Form(""),
) -> dict[str, Any]:
    """工作流 C 的入口：拖入任意来源的粗糙网格，只跑后处理，零 API 成本、永久免费。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in MESH_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的网格格式 {suffix}。支持：{'、'.join(sorted(MESH_SUFFIXES))}",
        )

    spec = _resolve_preset(preset_key)
    asset = store.create_asset(name=name, spec=spec, source=AssetSource.MESH.value)
    target = store.write_source_bytes(asset.id, Path(file.filename or "input.glb").name, await file.read())

    # 立刻建一个 import 版本节点，管线才能从它往后接
    node = VersionNode(
        asset_id=asset.id,
        parent_id=None,
        op=VersionOp.IMPORT,
        label="导入原始网格",
        params={"source": str(target)},
        mesh_path=str(target),
    )

    # FBX 没有纯 Python 的可靠读取方案，导入时经 Blender 转成 GLB 工作副本：
    # 原始 FBX 留在 source/ 只读，管线与视口全程只见 GLB（见 tools/convert.py）
    mesh_path = target
    if suffix == ".fbx":
        try:
            mesh_path = convert_to_glb(target, store.version_dir(asset.id, node.id))
        except MeshError as exc:
            telemetry.record("import_mesh", asset_id=asset.id, suffix=suffix, ok=False)
            store.archive_asset(asset.id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        node.params["converted_from"] = str(target)
        node.label = "导入 FBX（已转 GLB 工作副本）"

    node.mesh_path = str(mesh_path)
    store.add_version(node)
    store.patch_asset(asset.id, status=AssetStatus.PROCESSING)
    telemetry.record(
        "create_asset", asset_id=asset.id, source=asset.source
    )
    telemetry.record("import_mesh", asset_id=asset.id, suffix=suffix, ok=True)
    return _asset_summary(store.get_asset(asset.id))


def _resolve_preset(preset_key: str) -> SpecPreset:
    presets = load_spec_presets()
    if not preset_key:
        return next(iter(presets.values()))
    if preset_key not in presets:
        raise HTTPException(
            status_code=400,
            detail=f"未知的规格预设「{preset_key}」。可用：{'、'.join(sorted(presets))}",
        )
    return presets[preset_key]


# ------------------------------------------------------------------ 生成


@router.post("/{asset_id}/generate", status_code=202)
async def generate_variants(asset_id: str, body: GenerateBody) -> dict[str, Any]:
    asset = _require_asset(asset_id)
    settings = get_settings()

    try:
        provider = registry.resolve(body.provider)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    num_variants = max(1, min(6, body.num_variants))
    # 预估走已解析 Provider 自己的算法：离线占位与本地模型都无边际成本，
    # 用全局单价 × 次数会给用户报一个不存在的价格。
    estimate = provider.estimate_cost(num_variants, settings.cost_per_generation)
    if settings.budget_exceeded(estimate):
        raise HTTPException(
            status_code=402,
            detail=(
                f"本次生成预计花费 ¥{estimate:.2f}，加上本月已用额度会超出预算上限 "
                f"¥{float(settings.effective('monthly_budget_cny', 200) or 0):.2f}。"
                "请到「设置」调整预算上限，或减少变体数量。"
            ),
        )

    images = _image_sources(asset)
    # 既没有参考图也没有文字描述时，生成是没有依据的 —— 不管走哪家 Provider 都该拦下，
    # 而不是把一个空请求丢给 Provider 去炸（错误信息会难懂得多）。
    if not images and not (body.prompt or asset.enhanced_prompt or asset.prompt).strip():
        raise HTTPException(
            status_code=400,
            detail="这个资产还没有概念图，也没有文字描述，无法生成。请先上传参考图或填写描述。",
        )

    store.patch_asset(asset_id, status=AssetStatus.GENERATING)
    batch_dir = store.new_artifact_dir(asset_id, "variants", "batch")
    unit_cost = estimate / num_variants if num_variants else 0.0

    async def work(progress) -> None:
        generate_started = time.monotonic()
        request = GenerateRequest(
            asset_id=asset_id,
            out_dir=batch_dir,
            image_paths=images,
            # 用户手输 > AI 优化词 > 原始描述
            prompt=body.prompt or asset.enhanced_prompt or asset.prompt,
            spec=asset.spec,
            num_variants=num_variants,
            on_progress=lambda p, message: progress(0.05 + p * 0.8, message),
        )
        try:
            results = await provider.generate(request)
        except ProviderError:
            store.patch_asset(asset_id, status=AssetStatus.FAILED)
            raise

        for index, result in enumerate(results):
            await _persist_variant(asset_id, result, unit_cost)
            progress(0.85 + (index + 1) / len(results) * 0.1, f"已保存变体 {index + 1}")

        store.patch_asset(asset_id, status=AssetStatus.AWAITING_PICK)
        settings.record_usage(
            {
                "at": _timestamp(),
                "asset_id": asset_id,
                "provider": provider.name,
                "variants": len(results),
                "cost": estimate,
            }
        )
        telemetry.record(
            "generate",
            asset_id=asset_id,
            provider=provider.name,
            variants=len(results),
            cost=estimate,
            duration_ms=int((time.monotonic() - generate_started) * 1000),
        )

    job = await runner.submit(asset_id, JobStep.GENERATE, work)
    return {"job": job.model_dump(mode="json"), "provider": provider.name, "estimate_cny": estimate}


async def _persist_variant(asset_id: str, result, unit_cost: float) -> Variant:
    """把 Provider 产出的网格收进独立目录，并顺手出缩略图。

    每个变体独占一个目录 —— 后面"变体对比""版本回滚""重跑管线"都靠这个布局保持简单。
    """
    variant = Variant(asset_id=asset_id, provider=result.provider, params=result.params, mesh_path="")
    directory = store.variant_dir(asset_id, variant.id)
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"mesh{Path(result.mesh_path).suffix or '.glb'}"
    shutil.copy2(result.mesh_path, target)
    variant.mesh_path = str(target)
    variant.cost = round(unit_cost, 4)

    try:
        mesh = load_mesh(target)
        variant.face_count = int(len(mesh.faces))
        thumb = render_thumbnail(mesh, directory / "thumbnail.png", 256)
        variant.thumbnail_path = thumb.get("path")
    except Exception as exc:  # 缩略图失败不该让整个生成任务失败
        variant.params.setdefault("warnings", []).append(f"缩略图渲染失败：{exc}")

    store.add_variant(variant)

    # 变体同时是一个版本节点（op=generate），这样"变体对比"和"版本回滚"共用一套机制
    node = VersionNode(
        asset_id=asset_id,
        parent_id=None,
        op=VersionOp.GENERATE,
        label=f"变体 · {result.provider}",
        params=result.params,
        mesh_path=str(target),
        stats={"face_count": variant.face_count, "variant_id": variant.id},
    )
    store.add_version(node)
    return variant


@router.post("/{asset_id}/pick")
async def pick_variant(asset_id: str, body: PickVariantBody) -> dict[str, Any]:
    """美术挑中一个变体 —— 只有挑选之后才进后处理（见 5.3 变体制）。"""
    _require_asset(asset_id)
    try:
        variant = store.get_variant(asset_id, body.variant_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # 采纳率埋点：分母是本资产生成的变体总数，分子是这次挑选
    variants = store.list_variants(asset_id)
    telemetry.record(
        "pick_variant",
        asset_id=asset_id,
        variant_id=variant.id,
        provider=variant.provider,
        variant_index=next(
            (i for i, v in enumerate(variants) if v.id == variant.id), None
        ),
        total_variants=len(variants),
    )

    store.patch_asset(
        asset_id,
        picked_variant_id=variant.id,
        status=AssetStatus.PROCESSING,
    )
    return _asset_summary(store.get_asset(asset_id))


# ------------------------------------------------------------------ 管线 / 校验 / 导出


@router.post("/{asset_id}/pipeline", status_code=202)
async def run_post_pipeline(asset_id: str, body: PipelineBody) -> dict[str, Any]:
    _require_asset(asset_id)
    steps = body.steps or None

    async def work(progress) -> None:
        await run_pipeline(asset_id, progress, steps)

    job = await runner.submit(asset_id, JobStep.PIPELINE, work)
    return {"job": job.model_dump(mode="json")}


@router.post("/{asset_id}/export")
async def export(asset_id: str, body: ExportBody) -> dict[str, Any]:
    asset = _require_asset(asset_id)

    head = store.head_version(asset_id)
    if head is None:
        raise HTTPException(status_code=400, detail="这个资产还没有任何版本，无法导出。")

    report = store.latest_report(asset_id, head.id)
    if body.validate_first and report is not None and not report.passed and not body.allow_failed_export:
        failed = "、".join(r.label for r in report.failures)
        raise HTTPException(
            status_code=409,
            detail=(
                f"校验未通过（{failed}），导出被拦截。"
                "你可以选择「自动修复后重跑管线」，或在导出面板里勾选「忽略校验结果继续导出」。"
            ),
        )

    mesh_path = Path(head.mesh_path)
    if not mesh_path.exists():
        raise HTTPException(status_code=500, detail="版本文件丢失，请重跑管线。")

    out_dir = store.new_artifact_dir(asset_id, "exports", "exp")
    textures = sorted((store.asset_dir(asset_id) / "textures").glob("*.png"))
    export_started = time.monotonic()

    try:
        result = export_asset(
            load_mesh(mesh_path),
            out_dir,
            body.preset,
            asset_name=asset.name,
            texture_files=textures,
            source_mesh_path=mesh_path,
        )
    except Exception as exc:
        telemetry.record(
            "export",
            asset_id=asset_id,
            version_id=head.id,
            preset=body.preset,
            engine=telemetry.engine_from_preset(body.preset),
            ok=False,
            error_class=telemetry.error_class(exc),
        )
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc

    record = ExportRecord(
        asset_id=asset_id,
        version_id=head.id,
        preset=result["preset"],
        files=result["files"],
        warnings=result["warnings"],
    )
    store.add_export(record)
    store.patch_asset(asset_id, status=AssetStatus.EXPORTED)
    telemetry.record(
        "export",
        asset_id=asset_id,
        version_id=head.id,
        preset=result["preset"],
        engine=telemetry.engine_from_preset(result["preset"]),
        files=len(result["files"]),
        duration_ms=int((time.monotonic() - export_started) * 1000),
        ok=True,
    )

    return {
        "export": record.model_dump(mode="json"),
        "warnings": result["warnings"],
        "manifest": result["manifest"],
    }


# ------------------------------------------------------------------ 其它


@router.post("/{asset_id}/rollback")
async def rollback_to_version(asset_id: str, body: RollbackBody) -> dict[str, Any]:
    """非破坏性回滚：把目标版本的网格复制成**新的当前版本**（op=rollback）。

    不删任何历史、不移动任何旧节点 —— 版本树只增不改，这是 store 的铁律。
    回滚后资产回到 PROCESSING：可以直接导出（head 即回滚产物），
    也可以重跑管线从头再处理一遍。
    """
    _require_asset(asset_id)
    try:
        target = store.get_version(asset_id, body.version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    mesh_path = Path(target.mesh_path)
    if not mesh_path.exists():
        raise HTTPException(
            status_code=409,
            detail="目标版本的网格文件已丢失，无法回滚。请选择其它版本，或重跑管线生成新版本。",
        )

    node = VersionNode(
        asset_id=asset_id,
        parent_id=target.id,
        op=VersionOp.ROLLBACK,
        label=f"回滚自 {target.op}",
        params={"rolled_back_from": target.id, "source_op": target.op},
        mesh_path="",
    )
    directory = store.version_dir(asset_id, node.id)
    directory.mkdir(parents=True, exist_ok=True)

    node.mesh_path = str(directory / "mesh.glb")
    shutil.copy2(mesh_path, node.mesh_path)
    # OBJ 副本是四边面/n-gon 规则的测量文件：原版本有就带上，没有就清掉引用
    node.stats = {k: v for k, v in target.stats.items() if k not in {"obj_path", "obj_error"}}
    source_obj = target.stats.get("obj_path")
    if source_obj and Path(source_obj).exists():
        shutil.copy2(source_obj, directory / "mesh.obj")
        node.stats["obj_path"] = str(directory / "mesh.obj")

    store.add_version(node)
    store.patch_asset(asset_id, status=AssetStatus.PROCESSING)
    telemetry.record(
        "rollback", asset_id=asset_id, version_id=target.id, new_version_id=node.id
    )
    return _asset_summary(store.get_asset(asset_id))


@router.post("/{asset_id}/archive")
async def archive(asset_id: str) -> dict[str, Any]:
    """归档。只移动目录，不删除任何文件。"""
    _require_asset(asset_id)
    return _asset_summary(store.archive_asset(asset_id))


@router.post("/{asset_id}/retry")
async def retry(asset_id: str) -> dict[str, Any]:
    """失败后重试：把状态推回待挑选/处理中，保留已有产物。"""
    _require_asset(asset_id)
    target = AssetStatus.AWAITING_PICK if store.list_variants(asset_id) else AssetStatus.DRAFT
    return _asset_summary(store.patch_asset(asset_id, status=target))


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()

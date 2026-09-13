"""资产相关的 REST 接口。

这一层刻意做得很薄：只做参数校验、状态推进和任务提交，业务逻辑全在 store / tools / providers 里。
"""

from __future__ import annotations

import contextlib
import shutil
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import store, telemetry
from ..ai import llm as llm_module
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
    JobStatus,
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
from ..tools.pipeline import _run_off_loop

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
    elif asset.kind == "image":
        # 2D 图片资产：缩略图 = 最新生成图，否则源图
        renders = sorted((store.asset_dir(asset.id) / "renders").glob("*.png"))
        if renders:
            thumbnail = str(renders[-1])
        elif asset.source_files:
            source = Path(asset.source_files[-1])
            thumbnail = str(source) if source.exists() else None
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


def _discard_broken_asset(asset_id: str) -> None:
    """创建/导入失败的资产直接删掉（刚建的、不可用，归档只会让库里积灰）。

    删除失败不掩盖原始错误 —— 400 的 detail 才是用户要看的。
    """
    with contextlib.suppress(Exception):
        store.delete_asset(asset_id)


def _import_spec(preset_key: str) -> SpecPreset:
    """导入路径的规格：预设只取工程约定（引擎/轴心/单位等），**面数不限**。

    用户导入的模型是成品，不是我们控制生成的 —— 削减面数只会毁掉它
    （摩托实测 17.6 万面被压到 5000 = 碎车）。生成流程仍走预设预算。
    """
    return _resolve_preset(preset_key).model_copy(update={"face_budget": None})


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
        kind=body.kind,
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
        _discard_broken_asset(asset.id)
        raise HTTPException(status_code=400, detail="没有读到任何有效的图片内容。")

    telemetry.record("create_asset", asset_id=asset.id, source=asset.source, images=saved)
    return _asset_summary(store.get_asset(asset.id))


@router.post("/import-mesh", status_code=201)
async def create_asset_from_mesh(
    file: UploadFile = File(...),
    name: str = Form("未命名资产"),
    preset_key: str = Form(""),
) -> dict[str, Any]:
    """工作流 C 的入口（同步版，兼容旧调用）：导入网格并返回，管线由前端另发任务。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in MESH_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的网格格式 {suffix}。支持：{'、'.join(sorted(MESH_SUFFIXES))}",
        )

    spec = _import_spec(preset_key)
    asset = store.create_asset(name=name, spec=spec, source=AssetSource.MESH.value)
    target = store.write_source_bytes(asset.id, Path(file.filename or "input.glb").name, await file.read())

    try:
        node = _build_import_node(asset.id, target, suffix)
    except MeshError as exc:
        telemetry.record("import_mesh", asset_id=asset.id, suffix=suffix, ok=False)
        _discard_broken_asset(asset.id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    store.add_version(node)
    store.patch_asset(asset.id, status=AssetStatus.PROCESSING)
    telemetry.record("create_asset", asset_id=asset.id, source=asset.source)
    telemetry.record("import_mesh", asset_id=asset.id, suffix=suffix, ok=True)
    return _asset_summary(store.get_asset(asset.id))


def _build_import_node(asset_id: str, target: Path, suffix: str) -> VersionNode:
    """为导入的网格建立 import 版本节点（FBX 就地转 GLB 工作副本）。同步，重。"""
    node = VersionNode(
        asset_id=asset_id,
        parent_id=None,
        op=VersionOp.IMPORT,
        label="导入原始网格",
        params={"source": str(target)},
        mesh_path=str(target),
    )
    mesh_path = target
    if suffix == ".fbx":
        # FBX 没有纯 Python 的可靠读取方案，导入时经 ufbx2obj/Blender 转成 GLB：
        # 原始 FBX 留在 source/ 只读，管线与视口全程只见 GLB（见 tools/convert.py）
        mesh_path = convert_to_glb(target, store.version_dir(asset_id, node.id))
        node.params["converted_from"] = str(target)
        node.label = "导入 FBX（已转 GLB 工作副本）"
    node.mesh_path = str(mesh_path)
    if suffix in (".glb", ".gltf", ".fbx"):
        with contextlib.suppress(Exception):
            mesh = load_mesh(mesh_path)
            node.params["source_material_count"] = int(
                mesh.metadata.get("source_material_count", 0)
            )
            node.params["source_image_count"] = int(mesh.metadata.get("source_image_count", 0))
    return node


@router.post("/import", status_code=202)
async def import_assets(
    files: list[UploadFile] = File(...),
    name: str = Form(""),
    preset_key: str = Form(""),
) -> dict[str, Any]:
    """工作台「导入」入口：2D/3D 素材批量导入，**后台队列**自动处理。

    - 3D 网格（glb/gltf/obj/fbx/ply/stl）：建资产 → 后台任务完成 FBX 转换与
      整条后处理管线，完成后状态推到 已校验/待校验；
    - 2D 图片（png/jpg/webp/bmp）：建 2D 资产（kind=image），后台校验后即可用。

    返回每个文件的资产与任务 id —— 前端轮询任务，完成后通知并引导跳转预览/编辑。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到任何文件。")
    imports: list[dict[str, Any]] = []

    for file in files:
        filename = Path(file.filename or "未命名.asset").name
        suffix = Path(filename).suffix.lower()
        single_name = name.strip() if len(files) == 1 and name.strip() else filename.rsplit(".", 1)[0]

        if suffix in MESH_SUFFIXES:
            spec_model = _import_spec(preset_key)
            asset = store.create_asset(
                name=single_name, spec=spec_model, source=AssetSource.MESH.value, kind="model"
            )
            target = store.write_source_bytes(asset.id, filename, await file.read())
            store.patch_asset(asset.id, status=AssetStatus.PROCESSING)

            async def work(progress, *, asset_id=asset.id, path=target, suffix=suffix) -> None:
                try:
                    progress(0.05, "解析源文件…")
                    node = await _run_off_loop(_build_import_node, asset_id, path, suffix)
                    store.add_version(node)
                    progress(0.25, "后处理管线…")
                    await run_pipeline(asset_id, progress)
                except Exception:
                    store.patch_asset(asset_id, status=AssetStatus.FAILED)
                    raise

            job = await runner.submit(asset.id, JobStep.PIPELINE, work)
            imports.append(
                {"kind": "model", "asset": _asset_summary(store.get_asset(asset.id)), "job": job.model_dump(mode="json")}
            )
            telemetry.record("create_asset", asset_id=asset.id, source=asset.source)
            telemetry.record("import_queue", asset_id=asset.id, suffix=suffix, ok=True)

        elif suffix in IMAGE_SUFFIXES:
            asset = store.create_asset(
                name=single_name, source=AssetSource.IMAGE.value, kind="image"
            )
            store.write_source_bytes(asset.id, filename, await file.read())
            store.patch_asset(asset.id, status=AssetStatus.PROCESSING)

            async def work(progress, *, asset_id=asset.id, path=None, filename=filename) -> None:
                progress(0.5, "校验图片…")
                source = store.source_dir(asset_id) / filename
                await _run_off_loop(_validate_image, source)
                store.patch_asset(asset_id, status=AssetStatus.VALIDATED)

            job = await runner.submit(asset.id, JobStep.PIPELINE, work)
            imports.append(
                {"kind": "image", "asset": _asset_summary(store.get_asset(asset.id)), "job": job.model_dump(mode="json")}
            )
            telemetry.record("create_asset", asset_id=asset.id, source=asset.source)
            telemetry.record("import_queue", asset_id=asset.id, suffix=suffix, ok=True)

        else:
            imports.append(
                {"kind": "unsupported", "filename": filename, "error": f"不支持的格式 {suffix}"}
            )

    return {"imports": imports}


def _validate_image(path: Path) -> None:
    """图片导入校验：能打开且尺寸合理。"""
    from PIL import Image

    if not path.exists():
        raise ValueError(f"图片文件丢失：{path.name}")
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
        if w < 8 or h < 8:
            raise ValueError(f"图片尺寸过小（{w}×{h}）")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"图片无法读取（{type(exc).__name__}），文件可能已损坏。") from exc


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
    if asset.kind == "image":
        raise HTTPException(
            status_code=400,
            detail="这是 2D 图片素材：请使用「生成图片」（generate-image）而不是 3D 模型生成。",
        )
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


@router.delete("/{asset_id}")
async def delete(asset_id: str) -> dict[str, Any]:
    """删除资产：连目录带所有产物一起移除，不可恢复（前端有二次确认）。

    有任务在跑时拒绝删除 —— 否则后台任务会往被删目录里写文件，产出僵尸资产。
    """
    _require_asset(asset_id)
    running = [
        job for job in store.list_jobs(asset_id)
        if runner.is_running(job.id) and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    ]
    if running:
        raise HTTPException(
            status_code=409,
            detail="这个资产还有任务在后台处理，请等它结束（或取消任务）后再删除。",
        )
    store.delete_asset(asset_id)
    telemetry.record("delete_asset", asset_id=asset_id)
    return {"deleted": True, "asset_id": asset_id}


@router.post("/{asset_id}/retry")
async def retry(asset_id: str) -> dict[str, Any]:
    """失败后重试：把状态推回待挑选/处理中，保留已有产物。"""
    _require_asset(asset_id)
    target = AssetStatus.AWAITING_PICK if store.list_variants(asset_id) else AssetStatus.DRAFT
    return _asset_summary(store.patch_asset(asset_id, status=target))


# ------------------------------------------------------------------ 2D 图片素材


@router.post("/{asset_id}/generate-image", status_code=202)
async def generate_image(asset_id: str, body: GenerateBody) -> dict[str, Any]:
    """2D 图片素材生成：LLM 优化提示词 → CogView 出图 → 落为版本节点（后台任务）。"""
    asset = _require_asset(asset_id)
    if asset.kind != "image":
        raise HTTPException(status_code=400, detail="只有 2D 图片素材才能生成图片，3D 资产请使用模型生成。")
    if not (body.prompt or asset.enhanced_prompt or asset.prompt).strip():
        raise HTTPException(status_code=400, detail="没有生成依据：请先输入描述，或先使用 AI 优化描述。")

    store.patch_asset(asset_id, status=AssetStatus.GENERATING)
    from ..ai import imagegen as imagegen_module
    from ..ai.llm import LLMError

    async def work(progress) -> None:
        progress(0.05, "AI 优化提示词…")
        raw_prompt = (body.prompt or asset.enhanced_prompt or asset.prompt).strip()
        try:
            optimized = await llm_module.chat_json(
                "你是 2D 游戏素材的提示词优化助手。知识库要点：单一主体、完整可见、"
                "纯色背景、无文字水印、风格词具体（stylized/hand-painted/pixel art 等）。"
                "把用户的描述优化成一段图像生成提示词（中文），只输出 JSON："
                '{"prompt": "优化后的完整描述"}',
                f"用户描述：{raw_prompt}",
            )
            prompt = str(optimized.get("prompt") or "").strip() or raw_prompt
        except LLMError:
            prompt = raw_prompt  # 优化失败不阻塞生成，直接用原描述

        renders = store.asset_dir(asset_id) / "renders"
        renders.mkdir(parents=True, exist_ok=True)
        index = len(list(renders.glob("img_*.png"))) + 1
        progress(0.2, "生成图片…")
        image_path = await imagegen_module.generate_image(
            prompt, renders / f"img_{index:03d}.png", size="1024x1024"
        )

        node = VersionNode(
            asset_id=asset_id,
            parent_id=None,
            op=VersionOp.GENERATE,
            label=f"2D 生成 · {imagegen_module.imagegen_model()}",
            params={"prompt": prompt, "image": str(image_path), "source_prompt": raw_prompt},
            mesh_path="",
        )
        store.add_version(node)
        store.patch_asset(asset_id, status=AssetStatus.VALIDATED)
        telemetry.record(
            "generate_image", asset_id=asset_id, version_id=node.id, prompt_chars=len(prompt), ok=True
        )
        progress(1.0, "图片生成完成")

    job = await runner.submit(asset_id, JobStep.GENERATE, work)
    return {"job": job.model_dump(mode="json")}


def _timestamp() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()

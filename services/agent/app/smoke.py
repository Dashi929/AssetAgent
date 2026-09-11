"""端到端冒烟测试：在**不需要任何 API Key** 的情况下跑通整条链路。

跑的是工作流 A 的完整形态（概念图 → 生成 → 挑选 → 管线 → 校验 → 导出），
只是生成那一步走 mock provider。用途：

- 每次改完管线，先跑它，确认"从图到引擎"这条线没断。
- 新同事 clone 下来，一条命令就知道环境对不对。

用法：
    python -m app.smoke
    python -m app.smoke --keep      # 保留产物目录，便于人工查看
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from . import store
from .config import get_settings, reset_settings_cache
from .jobs import ProgressReporter
from .models import AssetStatus, Job, JobStep, SpecPreset
from .presets import get_export_preset, load_spec_presets
from .providers import GenerateRequest, registry
from .tools import export_asset, load_mesh, run_pipeline


def make_concept_image(path: Path, size: int = 512) -> Path:
    """造一张假概念图 —— 冒烟测试不该依赖任何外部素材。"""
    image = Image.new("RGB", (size, size), (236, 234, 228))
    draw = ImageDraw.Draw(image)
    draw.rectangle([size * 0.3, size * 0.35, size * 0.7, size * 0.8], fill=(150, 146, 138))
    draw.polygon(
        [(size * 0.5, size * 0.15), (size * 0.72, size * 0.38), (size * 0.28, size * 0.38)],
        fill=(120, 118, 112),
    )
    draw.ellipse([size * 0.42, size * 0.5, size * 0.58, size * 0.66], fill=(96, 94, 90))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


async def run_smoke(data_dir: Path, variants: int = 2) -> dict:
    os.environ["ASSETAGENT_DATA_DIR"] = str(data_dir)
    os.environ["ASSETAGENT_ALLOW_MOCK_FALLBACK"] = "true"
    reset_settings_cache()
    registry.reload()

    settings = get_settings()
    presets = load_spec_presets()
    spec: SpecPreset = next(iter(presets.values()))
    spec.expected_size_m = 1.0  # 让"真实单位"这条规则有得测

    print(f"[1/7] 数据目录：{settings.data_dir}")

    asset = store.create_asset(name="SM_SmokeTest_Prop", spec=spec, source="image")
    concept = make_concept_image(data_dir / "input" / "concept.png")
    store.add_source_file(asset.id, concept)
    print(f"[2/7] 已建资产 {asset.id}，规格「{spec.name}」")

    provider = registry.resolve(None)
    print(f"[3/7] 生成引擎：{provider.display_name}（mode={provider.info().mode}）")

    batch_dir = store.new_artifact_dir(asset.id, "variants", "batch")
    request = GenerateRequest(
        asset_id=asset.id,
        out_dir=batch_dir,
        image_paths=[concept],
        prompt="",
        spec=spec,
        num_variants=variants,
        on_progress=lambda p, m: None,
    )
    results = await provider.generate(request)
    print(f"       产出 {len(results)} 个变体")

    from .routers.assets import _persist_variant

    saved = [await _persist_variant(asset.id, result, 0.0) for result in results]
    for variant in saved:
        print(f"       变体 {variant.id}：{variant.face_count} 三角面")

    store.patch_asset(asset.id, picked_variant_id=saved[0].id, status=AssetStatus.PROCESSING)
    print(f"[4/7] 已挑选变体 {saved[0].id}")

    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
    reporter = ProgressReporter(job)
    summary = await run_pipeline(asset.id, reporter)
    print("[5/7] 管线执行：")
    for step in summary["steps"]:
        detail = ""
        if step["step"] == "decimate":
            detail = f" {step.get('before_faces')} → {step.get('after_faces')} 面"
        elif step["step"] == "uv":
            detail = f" UV 岛 {step.get('uv_islands')}"
        elif step["step"] == "validate":
            detail = f" 通过={step.get('passed')} 统计={step.get('counts')}"
        skipped = step.get("skipped_reason")
        print(f"       - {step['step']}{detail}{'  [跳过] ' + str(skipped) if skipped else ''}")

    report = store.latest_report(asset.id)
    if report is None:
        raise RuntimeError("校验报告没有生成")
    print("[6/7] 校验报告：")
    for result in report.results:
        mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skipped": "SKIP"}[result.result.value]
        print(f"       [{mark:4}] {result.label}：{result.message}")

    head = store.head_version(asset.id)
    out_dir = store.new_artifact_dir(asset.id, "exports", "exp")
    textures = sorted((store.asset_dir(asset.id) / "textures").glob("*.png"))
    exported = export_asset(
        load_mesh(head.mesh_path),
        out_dir,
        "unity",
        asset_name=asset.name,
        texture_files=textures,
        source_mesh_path=Path(head.mesh_path),
    )
    print("[7/7] 导出：")
    for path in exported["files"]:
        print(f"       - {path}")
    for warning in exported["warnings"]:
        print(f"       ⚠ {warning}")

    return {
        "asset_id": asset.id,
        "variants": len(saved),
        "versions": len(store.list_versions(asset.id)),
        "report_passed": report.passed,
        "report_counts": {
            r.result.value: sum(1 for x in report.results if x.result == r.result) for r in report.results
        },
        "exports": exported["files"],
        "data_dir": str(settings.data_dir),
        "preset": get_export_preset("unity")["display_name"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AssetAgent 端到端冒烟测试")
    parser.add_argument("--keep", action="store_true", help="保留产物目录")
    parser.add_argument("--dir", default="", help="指定数据目录（默认用临时目录）")
    parser.add_argument("--variants", type=int, default=2, help="生成变体数量")
    args = parser.parse_args()

    if args.dir:
        data_dir = Path(args.dir).resolve()
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="assetagent-smoke-"))

    print("=" * 68)
    print("AssetAgent 冒烟测试：概念图 → 生成 → 管线 → 校验 → 导出")
    print("=" * 68)

    try:
        result = asyncio.run(run_smoke(data_dir, args.variants))
    except Exception as exc:
        print(f"\n❌ 冒烟测试失败：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    print("=" * 68)
    print(f"✅ 全链路跑通。校验通过：{result['report_passed']}")
    print(f"   资产 {result['asset_id']}｜变体 {result['variants']}｜版本节点 {result['versions']}")
    print(f"   产物目录：{result['data_dir']}")
    print("=" * 68)

    if not args.keep and not args.dir:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("（临时产物已清理；加 --keep 可保留查看）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

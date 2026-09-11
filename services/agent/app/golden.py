"""黄金概念图集批量回归 —— M2/M3 验收与 Release Gate（G1/G2）的执行工具。

对 samples/golden/ 里冻结的 10 张概念图，逐张走完
"概念图 → 生成（mock，零 Key）→ 挑选 → 管线 → 校验 → 导出"，
输出每张的通过情况与耗时，最后给汇总：通过率、校验一次通过率、总耗时。

用法（在 services/agent/ 下）：
    .venv/Scripts/python -m app.golden
    .venv/Scripts/python -m app.golden --keep     # 保留产物目录人工查看
    .venv/Scripts/python -m app.golden --only 3   # 只跑第 3 张（定位问题用）

退出码：全部成功 0，任一失败 1 —— 可直接挂 CI。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import store
from .config import reset_settings_cache
from .jobs import ProgressReporter
from .models import AssetStatus, Job, JobStep, SpecPreset
from .presets import load_spec_presets
from .providers import GenerateRequest, registry
from .tools import export_asset, load_mesh, run_pipeline

REPO_ROOT = Path(__file__).resolve().parents[3]  # services/agent/app -> 仓库根
DEFAULT_GOLDEN_DIR = REPO_ROOT / "samples" / "golden"
# 每张图用的规格预设：按文件名后缀映射，没有映射就用默认第一张
PRESET_HINTS = {"arch": "blocker", "stump": "foliage"}


def _asset_name(label: str, spec: SpecPreset) -> str:
    """按规格预设的命名规范起资产名（SM_xxx / SM_Blocker_xxx / SM_Foliage_xxx）。"""
    base = f"SM_{label.capitalize()}"
    pattern = str(spec.naming_pattern or r"^SM_[A-Za-z0-9_]+$")
    if re.match(pattern, base):
        return base
    # 类目专属前缀（如 ^SM_Blocker_[A-Za-z0-9_]+$）：取模式里的固定前缀直接拼
    fixed = re.match(r"\^(SM_[A-Za-z0-9]+_)", pattern)
    return f"{fixed.group(1)}{label.capitalize()}" if fixed else base


async def run_one(
    concept: Path, label: str, data_dir: Path, variants: int = 1
) -> dict:
    """对一张概念图跑完整工作流 A。返回结果摘要，异常不上抛（记失败继续下一张）。"""
    started = time.monotonic()
    result: dict = {"label": label, "concept": concept.name, "ok": False, "error": None}

    os.environ["ASSETAGENT_DATA_DIR"] = str(data_dir)
    reset_settings_cache()
    registry.reload()

    try:
        presets = load_spec_presets()
        hint = next((v for k, v in PRESET_HINTS.items() if k in concept.stem), "")
        spec: SpecPreset = presets[hint] if hint in presets else next(iter(presets.values()))

        asset = store.create_asset(name=_asset_name(label, spec), spec=spec, source="image")
        store.add_source_file(asset.id, concept)

        provider = registry.resolve(None)
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

        from .routers.assets import _persist_variant

        saved = [await _persist_variant(asset.id, r, 0.0) for r in results]
        store.patch_asset(asset.id, picked_variant_id=saved[0].id, status=AssetStatus.PROCESSING)

        job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
        await run_pipeline(asset.id, ProgressReporter(job))

        report = store.latest_report(asset.id)
        if report is None:
            raise RuntimeError("校验报告没有生成")

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

        result.update(
            ok=True,
            asset_id=asset.id,
            variants=len(saved),
            faces=saved[0].face_count,
            validation_passed=report.passed,
            validation_counts={
                r.result.value: sum(1 for x in report.results if x.result == r.result)
                for r in report.results
            },
            failed_rules=[r.label for r in report.failures],
            failed_details=[
                f"{r.label}：value={r.value} 阈值={r.threshold} —— {r.message}"
                for r in report.failures
            ],
            exports=len(exported["files"]),
            warnings=len(exported["warnings"]),
            duration_s=round(time.monotonic() - started, 1),
        )
    except Exception as exc:  # 单张失败不拖垮整批
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_s"] = round(time.monotonic() - started, 1)
    return result


async def run_batch(golden_dir: Path, data_dir: Path, only: list[int] | None = None) -> list[dict]:
    images = sorted(golden_dir.glob("golden_*.png"))
    if not images:
        raise FileNotFoundError(
            f"{golden_dir} 里没有 golden_*.png。先跑 `python scripts/make_golden_set.py` 生成。"
        )
    if only:
        images = [img for i, img in enumerate(images, 1) if i in only]
        if not images:
            raise ValueError(f"--only {only} 没匹配到任何图（共 {len(sorted(golden_dir.glob('golden_*.png')))} 张）")

    results = []
    for index, concept in enumerate(images, 1):
        label = concept.stem.split("_", 2)[-1]  # golden_03_crate -> crate
        print(f"[{index}/{len(images)}] {concept.name} …", flush=True)
        # 每张图独立数据目录：互不污染，失败也好隔离排查
        item_dir = data_dir / concept.stem
        result = await run_one(concept, label, item_dir)
        results.append(result)
        if result["ok"]:
            marks = " ".join(
                f"{k.upper()}={v}" for k, v in sorted(result["validation_counts"].items())
            )
            print(
                f"    ✅ {result['duration_s']}s · {result['faces']} 面 · 校验 {marks}"
                + ("" if result["validation_passed"] else " · 有 FAIL")
            )
            if result["failed_rules"]:
                print(f"       FAIL 规则：{'、'.join(result['failed_rules'])}")
                for detail in result.get("failed_details", []):
                    print(f"       · {detail}")
        else:
            print(f"    ❌ {result['duration_s']}s · {result['error']}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金概念图集批量回归")
    parser.add_argument("--golden-dir", default=str(DEFAULT_GOLDEN_DIR))
    parser.add_argument("--keep", action="store_true", help="保留产物目录（人工查看用）")
    parser.add_argument("--dir", default="", help="指定产物根目录（默认临时目录）")
    parser.add_argument("--only", type=int, nargs="*", help="只跑第 N 张（1 起）")
    args = parser.parse_args()

    data_dir = Path(args.dir).resolve() if args.dir else Path(tempfile.mkdtemp(prefix="assetagent-golden-"))

    print("=" * 68)
    print("黄金概念图集回归：概念图 → 生成(mock) → 挑选 → 管线 → 校验 → 导出")
    print("=" * 68)
    try:
        results = asyncio.run(run_batch(Path(args.golden_dir).resolve(), data_dir, args.only))
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}")
        return 1

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    validation_passed = sum(1 for r in results if r.get("validation_passed"))
    total_time = round(sum(r["duration_s"] for r in results), 1)
    print("=" * 68)
    print(f"工作流通过：{passed}/{total}    校验一次通过：{validation_passed}/{total}    总耗时 {total_time}s")
    print(f"产物目录：{data_dir}")
    print("=" * 68)
    if passed < total:
        for r in results:
            if not r["ok"]:
                print(f"   ❌ {r['concept']}：{r['error']}")
        return 1
    if not args.keep and not args.dir:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("（临时产物已清理；加 --keep 可保留查看）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""管线端到端测试（离线，不需要任何 API Key）。

这是最有价值的一个测试：它验证"概念图 → 生成 → 挑选 → 修复/减面/UV/烘焙 → 校验 → 导出"
整条链路在没有任何外部依赖时也能跑完，且产物真的落盘、真的可复现。
"""

from __future__ import annotations

import pytest

from app import store
from app.jobs import ProgressReporter
from app.models import AssetStatus, Job, JobStep, SpecPreset, VersionOp
from app.providers import GenerateRequest, registry
from app.routers.assets import _persist_variant
from app.tools import load_mesh, run_pipeline
from app.tools.mesh_io import component_count


async def _make_asset_with_variant(tmp_path, name: str = "SM_Pipeline_Prop"):
    spec = SpecPreset(name="道具 · 默认", expected_size_m=1.0, face_budget=2000)
    asset = store.create_asset(name=name, spec=spec, source="image")

    concept = tmp_path / "concept.png"
    concept.write_bytes(b"\x89PNG\r\n\x1a\n")  # mock provider 不读图内容，占位即可
    store.add_source_file(asset.id, concept)

    provider = registry.resolve(None)
    assert provider.name == "mock", "测试环境必须落到 mock provider，不允许联网"

    batch = store.new_artifact_dir(asset.id, "variants", "batch")
    results = await provider.generate(
        GenerateRequest(asset_id=asset.id, out_dir=batch, spec=spec, num_variants=1)
    )
    variant = await _persist_variant(asset.id, results[0], 0.0)
    store.patch_asset(asset.id, picked_variant_id=variant.id, status=AssetStatus.PROCESSING)
    return asset, variant


async def test_pipeline_runs_end_to_end(tmp_path):
    asset, variant = await _make_asset_with_variant(tmp_path)

    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
    summary = await run_pipeline(asset.id, ProgressReporter(job))

    steps = [s["step"] for s in summary["steps"]]
    assert steps == ["repair", "decimate", "uv", "bake", "validate"]

    # 版本树：每个改网格的步骤各留一个节点
    ops = [v.op for v in store.list_versions(asset.id)]
    assert VersionOp.REPAIR in ops
    assert VersionOp.DECIMATE in ops
    assert VersionOp.UV in ops
    assert VersionOp.BAKE in ops

    # 校验报告落盘
    report = store.latest_report(asset.id)
    assert report is not None
    assert len(report.results) >= 7

    # 状态推进到"已校验"或"待校验"，绝不是还在 processing
    assert store.get_asset(asset.id).status in (
        AssetStatus.VALIDATED,
        AssetStatus.AWAITING_VALIDATION,
    )


async def test_decimate_respects_face_budget(tmp_path):
    asset, _ = await _make_asset_with_variant(tmp_path)

    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
    summary = await run_pipeline(asset.id, ProgressReporter(job))

    decimate = next(s for s in summary["steps"] if s["step"] == "decimate")
    if decimate.get("skipped_reason"):
        pytest.skip(f"减面后端未安装，跳过预算断言：{decimate['skipped_reason']}")

    assert decimate["after_faces"] <= asset.spec.face_budget


async def test_pipeline_normalizes_pivot_and_units(tmp_path):
    """修复步骤必须把轴心和尺度归一 —— 否则"轴心/单位"两条规则在生成资产上必然 FAIL。"""
    asset, _ = await _make_asset_with_variant(tmp_path)

    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
    await run_pipeline(asset.id, ProgressReporter(job))

    head = store.head_version(asset.id)
    mesh = load_mesh(head.mesh_path)
    low, high = mesh.bounds

    assert abs(float(low[2])) < 0.02, "底面没有贴地"
    assert abs(float((low[0] + high[0]) / 2)) < 0.02, "X 方向没有居中"
    assert abs(float((low[1] + high[1]) / 2)) < 0.02, "Y 方向没有居中"
    assert abs(float(max(high - low)) - 1.0) < 0.05, "最长边没有缩放到规格期望尺寸"


async def test_workflow_c_imports_existing_mesh(tmp_path, dirty_glb):
    """工作流 C：任意来源的网格直接进管线，不依赖生成环节。"""
    spec = SpecPreset(face_budget=1500, expected_size_m=1.0)
    asset = store.create_asset(name="SM_Imported_Prop", spec=spec, source="mesh")

    from app.models import VersionNode

    store.add_version(
        VersionNode(
            asset_id=asset.id,
            op=VersionOp.IMPORT,
            label="导入原始网格",
            mesh_path=str(dirty_glb),
        )
    )

    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)
    summary = await run_pipeline(asset.id, ProgressReporter(job))

    repair = next(s for s in summary["steps"] if s["step"] == "repair")
    assert repair["actions"], "修复步骤应该对脏网格做了事情"

    # 游离组件应该被清掉（走我们自己的统计函数，它内部对缺图引擎的情况有兜底）
    head = store.head_version(asset.id)
    mesh = load_mesh(head.mesh_path)
    assert component_count(mesh) == 1


async def test_pipeline_requires_a_mesh(tmp_path):
    """没有变体也没有导入网格时，管线要给出人话错误，而不是崩在某个深层调用里。"""
    from app.tools.pipeline import PipelineError

    asset = store.create_asset(name="SM_Empty", spec=SpecPreset())
    job = Job(asset_id=asset.id, step=JobStep.PIPELINE)

    with pytest.raises(PipelineError, match="还没有可处理的网格"):
        await run_pipeline(asset.id, ProgressReporter(job))

"""本地埋点测试。

埋点必须做到两件事：
1. **绝不弄挂业务** —— record() 在任何异常下都不上抛；
2. **MVP 埋点口径齐全**（docs/ROADMAP.md）：任务/每步耗时、失败原因分类、
   生成次数与花费、变体采纳、校验明细、导出次数与引擎类型。

API 层走一条真实工作流（生成 → 挑选 → 管线 → 导出），断言每个埋点事件都落盘，
再用导出端点把原始 NDJSON 读回来。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from test_api import make_png_bytes, wait_for_job

from app import telemetry
from app.config import get_settings
from app.providers.base import ProviderError
from app.tools.mesh_io import MeshError

# ------------------------------------------------------------------ 单元


def test_record_writes_jsonl_and_summary_aggregates():
    telemetry.record("job_finished", asset_id="a1", status="failed", duration_ms=1500,
                     error_class="mesh_error")
    telemetry.record("job_finished", asset_id="a1", status="succeeded", duration_ms=500)
    telemetry.record("generate", asset_id="a1", provider="mock", variants=2, cost=1.5)
    telemetry.record("pick_variant", asset_id="a1", variant_id="v1",
                     variant_index=0, total_variants=2)
    telemetry.record("export", asset_id="a1", preset="unity", engine="unity", ok=True)

    data_dir = get_settings().data_dir
    assert (data_dir / "telemetry.jsonl").exists()

    summary = telemetry.summary()
    assert summary["tasks_total"] == 2
    assert summary["tasks_failed"] == 1
    assert summary["success_rate"] == 0.5
    assert summary["failure_categories"] == {"mesh_error": 1}
    assert summary["generations"] == 1
    assert summary["generated_variants"] == 2
    assert summary["cost_cny"] == 1.5
    assert summary["variant_picks"] == 1
    assert summary["exports"] == 1


def test_record_swallows_errors(monkeypatch, tmp_path):
    """埋点是旁路：写盘炸了也只能记日志，绝不能带垮业务调用方。"""
    def boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry, "read_events", lambda **kw: [])
    monkeypatch.setattr(
        type(get_settings()), "telemetry_file",
        property(lambda self: tmp_path / "no" / "dir" / "t.jsonl"),
    )
    # 不应抛出
    telemetry.record("anything", x=1)


def test_error_class_mapping():
    assert telemetry.error_class(None) is None
    assert telemetry.error_class(ProviderError("x")) == "provider_error"
    assert telemetry.error_class(MeshError("x")) == "mesh_error"
    assert telemetry.error_class(ValueError("x")) == "ValueError"
    assert telemetry.error_class(asyncio.CancelledError()) == "cancelled"


def test_engine_from_preset():
    assert telemetry.engine_from_preset("unity") == "unity"
    assert telemetry.engine_from_preset("unreal") == "unreal"
    assert telemetry.engine_from_preset("") == "generic"
    assert telemetry.engine_from_preset("custom_preset") == "custom_preset"


# ------------------------------------------------------------------ API 端到端


def _run_workflow_a(client: TestClient) -> str:
    """上传 → 生成 → 挑选 → 管线 → 导出，返回 asset_id。"""
    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("concept.png", make_png_bytes(), "image/png"))],
        data={"name": "埋点样例"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["asset"]["id"]

    started = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 2})
    assert started.status_code == 202, started.text
    assert wait_for_job(client, started.json()["job"]["id"])["status"] == "succeeded"

    detail = client.get(f"/api/assets/{asset_id}").json()
    variant_id = sorted(v["id"] for v in detail["variants"])[0]
    picked = client.post(f"/api/assets/{asset_id}/pick", json={"variant_id": variant_id})
    assert picked.status_code == 200, picked.text

    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    assert wait_for_job(client, pipeline.json()["job"]["id"])["status"] == "succeeded"

    exported = client.post(
        f"/api/assets/{asset_id}/export", json={"preset": "unity", "allow_failed_export": True}
    )
    assert exported.status_code == 200, exported.text
    return asset_id


def test_telemetry_events_flow_through_workflow(client):
    asset_id = _run_workflow_a(client)

    summary = client.get("/api/telemetry/summary").json()
    assert summary["generations"] == 1
    assert summary["generated_variants"] == 2
    assert summary["variant_picks"] == 1
    assert summary["exports"] == 1
    assert summary["tasks_total"] == 2  # generate + pipeline，本工作流只有这两个任务
    assert summary["success_rate"] is not None
    assert summary["success_rate"] == 1.0

    # 生成事件的耗时与花费被记录
    events = [_loads(e) for e in _raw_lines()]
    generate_events = [e for e in events if e.get("event") == "generate"]
    assert len(generate_events) == 1
    assert generate_events[0]["duration_ms"] >= 0
    assert generate_events[0]["variants"] == 2

    pick_events = [e for e in events if e.get("event") == "pick_variant"]
    assert len(pick_events) == 1
    assert pick_events[0]["total_variants"] == 2
    assert pick_events[0]["variant_index"] is not None

    export_events = [e for e in events if e.get("event") == "export"]
    assert len(export_events) == 1
    assert export_events[0]["engine"] == "unity"
    assert export_events[0]["ok"] is True

    # 校验明细：管线里 validate 步会记一条
    validation_events = [e for e in events if e.get("event") == "validation"]
    assert len(validation_events) == 1
    assert isinstance(validation_events[0]["passed"], bool)
    assert isinstance(validation_events[0]["failures"], list)

    # 管线每步耗时：repair/decimate/uv/bake/validate 至少有 ok=True 的记录
    step_events = [e for e in events if e.get("event") == "pipeline_step" and e.get("ok")]
    step_names = {e["step"] for e in step_events}
    assert {"repair", "validate"} <= step_names

    # 每条埋点都带 asset_id（除 job 级别也应有）
    for e in [ev for ev in events if ev.get("event") in {"generate", "pick_variant", "export"}]:
        assert e.get("asset_id") == asset_id


def test_telemetry_records_failure_classification(client):
    """管线失败（无网格可处理）→ job_finished 带稳定失败分类。"""
    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("concept.png", make_png_bytes(), "image/png"))],
        data={"name": "会失败"},
    )
    asset_id = uploaded.json()["asset"]["id"]

    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    job = wait_for_job(client, pipeline.json()["job"]["id"])
    assert job["status"] == "failed"

    events = [_loads(e) for e in _raw_lines()]
    failures = [e for e in events if e.get("event") == "job_finished" and e.get("status") == "failed"]
    assert failures, events
    assert failures[-1]["error_class"] == "pipeline_error"
    assert failures[-1]["duration_ms"] >= 0


def test_telemetry_export_endpoint_is_ndjson(client):
    _run_workflow_a(client)
    response = client.get("/api/telemetry/export")
    assert response.status_code == 200
    assert "x-ndjson" in response.headers["content-type"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert lines, "至少要有埋点行"
    names = {_loads(line).get("event") for line in lines}
    assert "generate" in names
    assert "export" in names

    # month 过滤：未来月份应为空
    empty = client.get("/api/telemetry/export", params={"month": "1999-01"}).text
    assert empty.strip() == ""


# ------------------------------------------------------------------ 辅助


def _raw_lines() -> list[str]:
    from app.config import get_settings

    path = get_settings().telemetry_file
    return path.read_text(encoding="utf-8").splitlines()


def _loads(line: str) -> dict:
    import json

    return json.loads(line)

"""本地埋点 —— MVP 必做（docs/ROADMAP.md 埋点一节）。

全部落 `data_dir/telemetry.jsonl`，**本地 JSONL、默认不上传**；alpha 期由用户
手动导出上报（Settings 页「导出埋点数据」按钮 / GET /api/telemetry/export）。

事件一览（每行一个 JSON 对象，at 为 UTC ISO 时间）：

| event          | 触发点                  | 关键字段                                        |
|----------------|-------------------------|-------------------------------------------------|
| job_started    | jobs.JobRunner          | asset_id, job_id, step                          |
| job_finished   | jobs.JobRunner          | + status, duration_ms, error_class, error       |
| pipeline_step  | tools/pipeline 每步     | step, duration_ms, ok, error_class              |
| generate       | routers/assets 生成完成 | provider, variants, cost, duration_ms           |
| pick_variant   | routers/assets 挑选     | variant_id, variant_index, total_variants       |
| validation     | pipeline 校验步         | version_id, passed, failures[], counts{}        |
| export         | routers/assets 导出     | preset, engine, files, ok                       |
| import_mesh    | routers/assets 工作流 C | suffix, ok                                      |
| create_asset   | routers/assets 建资产   | source（image/mesh/manual）                     |

设计红线：**埋点绝不能弄挂业务** —— record() 吞掉一切异常，只写 logger。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("assetagent.telemetry")


def _known_classes() -> tuple[tuple[type[BaseException], str], ...]:
    from .providers.base import ProviderError
    from .tools.mesh_io import MeshError
    from .tools.pipeline import PipelineError

    return (
        (ProviderError, "provider_error"),
        (MeshError, "mesh_error"),
        (PipelineError, "pipeline_error"),
    )


def error_class(exc: BaseException | None) -> str | None:
    """把异常映射成稳定的失败分类。CancelledError 不算失败（用户取消）。"""
    if exc is None:
        return None
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    for cls, name in _known_classes():
        if isinstance(exc, cls):
            return name
    return type(exc).__name__


def record(event: str, **fields: Any) -> None:
    """追加一条埋点。任何失败只记日志，绝不向上抛。"""
    from .config import get_settings

    try:
        entry = {"at": datetime.now(UTC).isoformat(), "event": event, **fields}
        path = get_settings().telemetry_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — 埋点是旁路，坏了不能带垮业务
        logger.warning("埋点写入失败 event=%s", event, exc_info=True)


def read_events(month: str | None = None) -> list[dict[str, Any]]:
    """读回全部埋点（month 形如 2026-09，按 at 前缀过滤）。坏行跳过。"""
    from .config import get_settings

    path = get_settings().telemetry_file
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if month and not str(entry.get("at", "")).startswith(month):
            continue
        events.append(entry)
    return events


def summary(month: str | None = None) -> dict[str, Any]:
    """Settings 页展示用的聚合视图。"""
    events = read_events(month=month)

    def _of(name: str) -> list[dict[str, Any]]:
        return [e for e in events if e.get("event") == name]

    jobs_done = _of("job_finished")
    failed = [e for e in jobs_done if e.get("status") == "failed"]
    durations = [float(e["duration_ms"]) / 1000 for e in jobs_done if e.get("duration_ms")]
    pipeline_steps = [e for e in _of("pipeline_step") if e.get("ok") is True]
    step_durations = [float(e["duration_ms"]) / 1000 for e in pipeline_steps if e.get("duration_ms")]

    failure_categories: dict[str, int] = {}
    for entry in failed:
        key = str(entry.get("error_class") or "unknown")
        failure_categories[key] = failure_categories.get(key, 0) + 1

    picks = _of("pick_variant")
    generates = _of("generate")
    generated_variants = sum(int(e.get("variants", 0) or 0) for e in generates)

    return {
        "month": month,
        "tasks_total": len(jobs_done),
        "tasks_failed": len(failed),
        "success_rate": round(1.0 - len(failed) / len(jobs_done), 4) if jobs_done else None,
        "avg_task_seconds": round(sum(durations) / len(durations), 2) if durations else None,
        "avg_pipeline_step_seconds": (
            round(sum(step_durations) / len(step_durations), 2) if step_durations else None
        ),
        "failure_categories": failure_categories,
        "generations": len(generates),
        "generated_variants": generated_variants,
        "cost_cny": round(sum(float(e.get("cost", 0) or 0) for e in generates), 4),
        "variant_picks": len(picks),
        "exports": len(_of("export")),
        "imports": len([e for e in _of("import_mesh") if e.get("ok") is True]),
    }


def engine_from_preset(preset: str) -> str:
    """预设键 → 引擎名（埋点口径：导出次数与引擎类型）。"""
    key = (preset or "").strip().lower()
    if "unity" in key:
        return "unity"
    if "unreal" in key or key in {"ue", "ue4", "ue5"}:
        return "unreal"
    if not key or key in {"generic", "default", "glb"}:
        return "generic"
    return key


__all__ = ["engine_from_preset", "error_class", "read_events", "record", "summary"]

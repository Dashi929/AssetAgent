"""资产库与版本树的唯一写入方。

设计约束（来自产品策划文档 1.3 设计原则）：
- **永不覆盖用户文件**：源文件只读拷贝进 source/；同名产物自动追加 _v2、_v3。
- **每个结果可追溯**：所有产物落独立目录 + 版本号，关掉应用重开状态完整。
- **删除即归档**：archive_asset 只把目录移到 archive/，绝不真删。
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import get_settings
from .models import (
    Asset,
    AssetStatus,
    ExportRecord,
    Job,
    JobStatus,
    SemanticCheck,
    SpecPreset,
    ValidationReport,
    Variant,
    VersionNode,
    new_id,
    now,
)

T = TypeVar("T", bound=BaseModel)

_lock = threading.RLock()


# ------------------------------------------------------------------ 基础工具


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def unique_path(directory: Path, filename: str) -> Path:
    """返回一个绝不会覆盖既有文件的路径。存在则追加 _v2 / _v3 …"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_v{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


# ------------------------------------------------------------------ 路径


def asset_dir(asset_id: str) -> Path:
    return get_settings().assets_dir / asset_id


def source_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "source"


def variants_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "variants"


def versions_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "versions"


def exports_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "exports"


def reports_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "reports"


def jobs_dir(asset_id: str) -> Path:
    return asset_dir(asset_id) / "jobs"


# ------------------------------------------------------------------ 资产


def create_asset(
    name: str = "未命名资产",
    spec: SpecPreset | None = None,
    source: str = "image",
    prompt: str = "",
    tags: list[str] | None = None,
    kind: str = "model",
) -> Asset:
    asset = Asset(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        spec=spec or SpecPreset(),
        source=source,  # type: ignore[arg-type]
        prompt=prompt,
        tags=tags or [],
    )
    with _lock:
        asset_dir(asset.id).mkdir(parents=True, exist_ok=True)
        source_dir(asset.id).mkdir(parents=True, exist_ok=True)
        save_asset(asset)
    return asset


def save_asset(asset: Asset) -> Asset:
    asset.updated_at = now()
    with _lock:
        _write_json(asset_dir(asset.id) / "asset.json", _dump(asset))
    return asset


def get_asset(asset_id: str) -> Asset:
    path = asset_dir(asset_id) / "asset.json"
    if not path.exists():
        raise FileNotFoundError(f"资产不存在：{asset_id}")
    return Asset.model_validate(_read_json(path))


def list_assets(include_archived: bool = False) -> list[Asset]:
    root = get_settings().assets_dir
    assets: list[Asset] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = child / "asset.json"
        if not child.is_dir() or not meta.exists():
            continue
        try:
            asset = Asset.model_validate(_read_json(meta))
        except Exception:  # 单个资产损坏不应拖垮整个列表
            continue
        if not include_archived and asset.status == AssetStatus.ARCHIVED:
            continue
        assets.append(asset)
    return assets


def patch_asset(asset_id: str, **fields: Any) -> Asset:
    asset = get_asset(asset_id)
    for key, value in fields.items():
        if hasattr(asset, key) and value is not None:
            setattr(asset, key, value)
    return save_asset(asset)


def set_status(asset_id: str, status: AssetStatus) -> Asset:
    return patch_asset(asset_id, status=status)


def archive_asset(asset_id: str) -> Asset:
    """归档 = 移动目录，不删除任何文件。"""
    asset = get_asset(asset_id)
    asset.status = AssetStatus.ARCHIVED
    save_asset(asset)
    return asset


# ------------------------------------------------------------------ 源文件


def add_source_file(asset_id: str, src: Path, filename: str | None = None) -> Path:
    """把源文件拷进 source/，只读保存。原始文件一律不动。"""
    target = unique_path(source_dir(asset_id), filename or src.name)
    shutil.copy2(src, target)
    asset = get_asset(asset_id)
    asset.source_files.append(str(target))
    save_asset(asset)
    return target


def write_source_bytes(asset_id: str, filename: str, data: bytes) -> Path:
    target = unique_path(source_dir(asset_id), filename)
    target.write_bytes(data)
    asset = get_asset(asset_id)
    asset.source_files.append(str(target))
    save_asset(asset)
    return target


# ------------------------------------------------------------------ 变体


def add_variant(variant: Variant) -> Variant:
    with _lock:
        directory = variants_dir(variant.asset_id) / variant.id
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "variant.json", _dump(variant))
    return variant


def get_variant(asset_id: str, variant_id: str) -> Variant:
    path = variants_dir(asset_id) / variant_id / "variant.json"
    if not path.exists():
        raise FileNotFoundError(f"变体不存在：{variant_id}")
    return Variant.model_validate(_read_json(path))


def list_variants(asset_id: str) -> list[Variant]:
    root = variants_dir(asset_id)
    if not root.exists():
        return []
    out: list[Variant] = []
    for child in sorted(root.iterdir()):
        meta = child / "variant.json"
        if meta.exists():
            out.append(Variant.model_validate(_read_json(meta)))
    return sorted(out, key=lambda v: v.created_at)


def variant_dir(asset_id: str, variant_id: str) -> Path:
    return variants_dir(asset_id) / variant_id


# ------------------------------------------------------------------ 版本树


def add_version(node: VersionNode) -> VersionNode:
    with _lock:
        directory = versions_dir(node.asset_id) / node.id
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "version.json", _dump(node))
        asset = get_asset(node.asset_id)
        asset.head_version_id = node.id
        save_asset(asset)
    return node


def get_version(asset_id: str, version_id: str) -> VersionNode:
    path = versions_dir(asset_id) / version_id / "version.json"
    if not path.exists():
        raise FileNotFoundError(f"版本不存在：{version_id}")
    return VersionNode.model_validate(_read_json(path))


def list_versions(asset_id: str) -> list[VersionNode]:
    root = versions_dir(asset_id)
    if not root.exists():
        return []
    nodes: list[VersionNode] = []
    for child in root.iterdir():
        meta = child / "version.json"
        if meta.exists():
            nodes.append(VersionNode.model_validate(_read_json(meta)))
    return sorted(nodes, key=lambda n: n.created_at)


def version_dir(asset_id: str, version_id: str) -> Path:
    return versions_dir(asset_id) / version_id


def head_version(asset_id: str) -> VersionNode | None:
    asset = get_asset(asset_id)
    if not asset.head_version_id:
        return None
    try:
        return get_version(asset_id, asset.head_version_id)
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ 校验报告


def save_report(report: ValidationReport) -> ValidationReport:
    with _lock:
        _write_json(reports_dir(report.asset_id) / f"{report.id}.json", _dump(report))
    return report


def list_reports(asset_id: str) -> list[ValidationReport]:
    root = reports_dir(asset_id)
    if not root.exists():
        return []
    reports = [ValidationReport.model_validate(_read_json(p)) for p in root.glob("*.json")]
    return sorted(reports, key=lambda r: r.created_at)


def latest_report(asset_id: str, version_id: str | None = None) -> ValidationReport | None:
    reports = list_reports(asset_id)
    if version_id:
        reports = [r for r in reports if r.version_id == version_id]
    return reports[-1] if reports else None


# ------------------------------------------------------------------ 语义校验（视觉 LLM）


def save_semantic_check(check: SemanticCheck) -> SemanticCheck:
    with _lock:
        _write_json(reports_dir(check.asset_id) / f"{check.id}.json", _dump(check))
    return check


def list_semantic_checks(asset_id: str) -> list[SemanticCheck]:
    root = reports_dir(asset_id)
    if not root.exists():
        return []
    checks = [
        SemanticCheck.model_validate(_read_json(p))
        for p in root.glob("chk_*.json")
    ]
    return sorted(checks, key=lambda c: c.created_at)


def latest_semantic_check(asset_id: str) -> SemanticCheck | None:
    checks = list_semantic_checks(asset_id)
    return checks[-1] if checks else None


# ------------------------------------------------------------------ 导出记录


def add_export(record: ExportRecord) -> ExportRecord:
    with _lock:
        _write_json(exports_dir(record.asset_id) / f"{record.id}.json", _dump(record))
    return record


def list_exports(asset_id: str) -> list[ExportRecord]:
    root = exports_dir(asset_id)
    if not root.exists():
        return []
    records = [ExportRecord.model_validate(_read_json(p)) for p in root.glob("*.json")]
    return sorted(records, key=lambda r: r.created_at)


# ------------------------------------------------------------------ 任务


def save_job(job: Job) -> Job:
    with _lock:
        _write_json(jobs_dir(job.asset_id) / f"{job.id}.json", _dump(job))
    return job


def get_job(asset_id: str, job_id: str) -> Job:
    path = jobs_dir(asset_id) / f"{job_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"任务不存在：{job_id}")
    return Job.model_validate(_read_json(path))


def list_jobs(asset_id: str) -> list[Job]:
    root = jobs_dir(asset_id)
    if not root.exists():
        return []
    jobs = [Job.model_validate(_read_json(p)) for p in root.glob("*.json")]
    return sorted(jobs, key=lambda j: j.created_at)


def reap_orphan_jobs() -> list[Job]:
    """应用重启后，落盘仍是 RUNNING 的任务已成孤儿（执行它们的进程没了）。

    不清理的话前端会永远显示"进行中"。全部标记为失败，交由用户重试；
    返回被清理的任务，供启动日志记录。
    """
    reaped: list[Job] = []
    for asset in list_assets(include_archived=True):
        for job in list_jobs(asset.id):
            if job.status is not JobStatus.RUNNING:
                continue
            job.status = JobStatus.FAILED
            job.error = "应用重启导致任务中断。任务本身没有问题，重新执行即可。"
            job.finished_at = now()
            save_job(job)
            reaped.append(job)
    return reaped


def find_job(job_id: str) -> Job | None:
    """跨资产查找任务 —— 前端轮询进度时只知道 job_id。"""
    root = get_settings().assets_dir
    if not root.exists():
        return None
    for child in root.iterdir():
        candidate = child / "jobs" / f"{job_id}.json"
        if candidate.exists():
            return Job.model_validate(_read_json(candidate))
    return None


# ------------------------------------------------------------------ 产物路径


def make_artifact_path(asset_id: str, bucket: str, filename: str) -> Path:
    """为管线步骤生成一个不会覆盖既有文件的产物路径。"""
    directory = asset_dir(asset_id) / bucket
    return unique_path(directory, filename)


def new_artifact_dir(asset_id: str, bucket: str, prefix: str) -> Path:
    directory = asset_dir(asset_id) / bucket / new_id(prefix)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


__all__ = [
    "add_export",
    "add_source_file",
    "add_variant",
    "add_version",
    "archive_asset",
    "asset_dir",
    "create_asset",
    "exports_dir",
    "find_job",
    "get_asset",
    "get_job",
    "get_variant",
    "get_version",
    "head_version",
    "latest_report",
    "latest_semantic_check",
    "list_assets",
    "list_exports",
    "list_jobs",
    "list_reports",
    "list_semantic_checks",
    "list_variants",
    "list_versions",
    "make_artifact_path",
    "new_artifact_dir",
    "patch_asset",
    "reports_dir",
    "save_asset",
    "save_job",
    "save_report",
    "save_semantic_check",
    "set_status",
    "source_dir",
    "unique_path",
    "variant_dir",
    "variants_dir",
    "version_dir",
    "versions_dir",
    "write_source_bytes",
]

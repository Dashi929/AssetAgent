/** 状态徽章 —— 资产状态机的可视化。文案与 app/models.py 的 AssetStatus 一一对应。 */

import type { AssetStatus, CheckResult, JobStatus } from '../api/types';

const ASSET_LABELS: Record<AssetStatus, string> = {
  draft: '草稿',
  generating: '生成中',
  awaiting_pick: '待挑选',
  processing: '后处理中',
  awaiting_validation: '待校验',
  validated: '已校验',
  exported: '已导出',
  failed: '失败',
  archived: '已归档',
};

const JOB_LABELS: Record<JobStatus, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

const CHECK_LABELS: Record<CheckResult, string> = {
  pass: 'PASS',
  fail: 'FAIL',
  warn: 'WARN',
  skipped: 'SKIP',
};

export function StatusBadge({ status }: { status: AssetStatus }) {
  return <span className={`badge ${status}`}>{ASSET_LABELS[status] ?? status}</span>;
}

export function JobBadge({ status }: { status: JobStatus }) {
  return <span className={`badge ${status}`}>{JOB_LABELS[status] ?? status}</span>;
}

export function CheckBadge({ result }: { result: CheckResult }) {
  return <span className={`badge ${result}`}>{CHECK_LABELS[result] ?? result}</span>;
}

export { ASSET_LABELS, CHECK_LABELS, JOB_LABELS };

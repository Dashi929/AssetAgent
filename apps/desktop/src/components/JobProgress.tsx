/**
 * 任务进度条。
 *
 * 生成与管线都可能是分钟级的操作，所以这里必须显示**具体在做什么**（message），
 * 而不只是一个百分比 —— "卡在 60% 不动了"和"正在烘焙 AO 贴图"是完全不同的体验。
 */

import type { Job } from '../api/types';
import { JobBadge } from './StatusBadge';

interface Props {
  job: Job | null;
  onCancel?: () => void;
  onDismiss?: () => void;
}

export function JobProgress({ job, onCancel, onDismiss }: Props) {
  if (!job) return null;

  const percent = Math.round((job.progress ?? 0) * 100);
  const done = job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled';

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row" style={{ marginBottom: 8 }}>
        <JobBadge status={job.status} />
        <span className="muted mono">{job.step}</span>
        <span className="spacer" />
        <span className="mono">{percent}%</span>
        {!done && onCancel && (
          <button onClick={onCancel} className="danger">
            取消
          </button>
        )}
        {done && onDismiss && <button onClick={onDismiss}>收起</button>}
      </div>

      <div className="progress">
        <span style={{ width: `${percent}%` }} />
      </div>

      <p className="muted" style={{ margin: '8px 0 0' }}>
        {job.error ? `❌ ${job.error}` : job.message || '准备中…'}
      </p>
    </div>
  );
}

export default JobProgress;

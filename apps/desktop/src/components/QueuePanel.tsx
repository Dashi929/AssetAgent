/**
 * 任务队列浮窗（资产库右下角）。
 *
 * 数据来自 GET /api/jobs/queue（进行中在前 + 最近完成的若干条，带资产名），
 * 轮询 1.2s 一次 —— 和工作台的 JobProgress（单任务条）互不影响，这里看全局队列。
 *
 * 显示逻辑：
 * - 有任务在跑 → 自动弹出；全部结束 → 保持显示最终状态，用户可关；
 * - × 关掉后，有**新**任务提交（活跃数增加）会自动重新弹出；
 * - 「—」折叠成小条，只留标题和进行中数量。
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Job, JobStep } from '../api/types';
import { JobBadge } from './StatusBadge';

type QueueJob = Job & { asset_name: string };

const POLL_MS = 1200;

const STEP_LABELS: Record<JobStep, string> = {
  generate: '生成',
  repair: '修复',
  decimate: '减面',
  uv: 'UV',
  bake: '烘焙',
  validate: '校验',
  export: '导出',
  render: '渲染',
  pipeline: '后处理管线',
};

const isActive = (job: QueueJob) => job.status === 'queued' || job.status === 'running';

export function QueuePanel() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<QueueJob[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const activeCountRef = useRef(0);

  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      try {
        const result = await api.getJobQueue();
        if (stopped) return;
        setJobs(result.jobs);
        // 活跃任务变多 = 有新任务提交 → 把关掉的浮窗重新弹出来
        const count = result.jobs.filter(isActive).length;
        if (count > activeCountRef.current) setDismissed(false);
        activeCountRef.current = count;
      } catch {
        // sidecar 未就绪等：保留上一次内容，静默重试即可
      }
    };
    void tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);

  if (dismissed || jobs.length === 0) return null;
  const activeCount = jobs.filter(isActive).length;

  return (
    <div className="queue-panel">
      <div className="queue-head">
        <strong>任务队列</strong>
        {activeCount > 0 ? (
          <span className="badge running">{activeCount} 进行中</span>
        ) : (
          <span className="badge pass">空闲</span>
        )}
        <span className="spacer" />
        <button
          title={collapsed ? '展开' : '折叠'}
          style={{ padding: '0 7px' }}
          onClick={() => setCollapsed(!collapsed)}
        >
          {collapsed ? '展开' : '—'}
        </button>
        <button title="关闭（有新任务会自动弹出）" style={{ padding: '0 7px' }} onClick={() => setDismissed(true)}>
          ×
        </button>
      </div>

      {!collapsed && (
        <div className="queue-list">
          {jobs.map((job) => {
            const percent = Math.round((job.progress ?? 0) * 100);
            const active = isActive(job);
            return (
              <div key={job.id} className="queue-row">
                <div className="row" style={{ gap: 6 }}>
                  <button
                    className="queue-name"
                    title={job.asset_name}
                    onClick={() => navigate(`/asset/${job.asset_id}`)}
                  >
                    {job.asset_name}
                  </button>
                  <JobBadge status={job.status} />
                  <span className="spacer" />
                  <span className="mono muted">
                    {STEP_LABELS[job.step] ?? job.step} · {percent}%
                  </span>
                  {active && (
                    <button
                      className="danger"
                      style={{ padding: '0 7px', fontSize: 12 }}
                      title="取消这个任务"
                      onClick={() => void api.cancelJob(job.id).catch(() => {})}
                    >
                      取消
                    </button>
                  )}
                </div>
                <div className="progress">
                  <span style={{ width: `${percent}%` }} />
                </div>
                <div className="queue-msg muted" title={job.error ?? job.message}>
                  {job.error ? `❌ ${job.error}` : job.message || '准备中…'}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default QueuePanel;

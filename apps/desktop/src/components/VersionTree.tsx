/**
 * 版本树。
 *
 * 变体和后处理步骤都是版本节点（op=generate / repair / decimate / uv / bake），
 * 所以"变体对比"和"版本回滚"在这里是同一个列表 —— 不需要两套界面逻辑。
 *
 * 回滚是**非破坏性**的：只是把某个节点的网格作为查看/后续操作的起点，不删任何历史。
 */

import type { VersionNode } from '../api/types';

const OP_LABELS: Record<string, string> = {
  import: '导入',
  generate: '生成',
  repair: '修复',
  decimate: '减面',
  uv: 'UV',
  bake: '烘焙',
  export: '导出',
  rollback: '回滚',
};

interface Props {
  versions: VersionNode[];
  activeVersionId?: string | null;
  headVersionId?: string | null;
  onSelect: (version: VersionNode) => void;
  /** 回滚到指定版本（非破坏性：产生一个新的当前版本）。缺省不显示按钮。 */
  onRollback?: (version: VersionNode) => void;
}

export function VersionTree({ versions, activeVersionId, headVersionId, onSelect, onRollback }: Props) {
  if (versions.length === 0) {
    return <div className="empty">还没有版本节点。生成或跑一次管线后就会出现在这里。</div>;
  }

  // 从旧到新，让"根 → 头"的推进方向符合直觉
  const ordered = [...versions].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );

  return (
    <div className="tree">
      {ordered.map((version, index) => {
        const isActive = version.id === activeVersionId;
        const isHead = version.id === headVersionId;
        return (
          <div
            key={version.id}
            className={`tree-node${isHead ? ' head' : ''}`}
            style={{ background: isActive ? 'var(--accent-soft)' : undefined }}
            onClick={() => onSelect(version)}
            title={version.skipped_reason ?? version.mesh_path}
          >
            <span className="muted">{String(index).padStart(2, '0')}</span>{' '}
            <strong>{OP_LABELS[version.op] ?? version.op}</strong>
            {version.label && version.label !== version.op ? ` · ${version.label}` : ''}
            {isHead && <span className="muted"> · 当前</span>}
            {version.skipped_reason && <span className="badge skipped" style={{ marginLeft: 6 }}>跳过</span>}
            {typeof version.stats?.face_count === 'number' && (
              <span className="muted"> · {version.stats.face_count} 面</span>
            )}
            {onRollback && !isHead && (
              <button
                style={{ marginLeft: 8, padding: '1px 8px', fontSize: 12 }}
                onClick={(event) => {
                  event.stopPropagation();
                  onRollback(version);
                }}
                title="把这个版本的网格复制为新的当前版本（不删除任何历史）"
              >
                回滚到此
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default VersionTree;

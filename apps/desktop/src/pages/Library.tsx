/**
 * 资产库。
 *
 * 网格视图 + 状态徽章。缩略图用 sidecar 渲好的 PNG（不是实时 3D），
 * 所以几百个资产也能秒开 —— 只有进详情页才加载 three.js 视口。
 */

import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { fileUrlSync } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import { StatusBadge } from '../components/StatusBadge';
import QueuePanel from '../components/QueuePanel';
import type { AssetStatus } from '../api/types';

const FILTERS: { key: AssetStatus | 'all'; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'awaiting_pick', label: '待挑选' },
  { key: 'awaiting_validation', label: '待校验' },
  { key: 'validated', label: '已校验' },
  { key: 'exported', label: '已导出' },
  { key: 'failed', label: '失败' },
];

export function Library() {
  const navigate = useNavigate();
  const { assets, busy } = useAppStore();
  const [filter, setFilter] = useState<AssetStatus | 'all'>('all');
  const [keyword, setKeyword] = useState('');

  const visible = useMemo(() => {
    const lower = keyword.trim().toLowerCase();
    return assets.filter((item) => {
      if (filter !== 'all' && item.asset.status !== filter) return false;
      if (lower && !item.asset.name.toLowerCase().includes(lower)) return false;
      return true;
    });
  }, [assets, filter, keyword]);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>资产库</h1>
          <div className="sub">
            共 {assets.length} 个资产
            {busy ? ' · 加载中…' : ''}
          </div>
        </div>
        <div className="row">
          <input
            style={{ width: 200 }}
            placeholder="按名称搜索"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
        </div>
      </div>

      <div className="row wrap" style={{ marginBottom: 14 }}>
        {FILTERS.map((item) => (
          <button
            key={item.key}
            className={filter === item.key ? 'primary' : ''}
            onClick={() => setFilter(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {visible.length === 0 ? (
        <div className="empty">
          还没有资产。到「工作台」拖一张概念图，或拖一个已有网格进来跑后处理。
        </div>
      ) : (
        <div className="grid">
          {visible.map(({ asset, counts, thumbnail, validation }) => (
            <button key={asset.id} className="asset-card" onClick={() => navigate(`/asset/${asset.id}`)}>
              <div className="thumb">
                {thumbnail ? (
                  <img src={fileUrlSync(thumbnail)} alt={asset.name} loading="lazy" />
                ) : (
                  <span className="muted">无缩略图</span>
                )}
              </div>
              <div className="meta">
                <div className="name" title={asset.name}>
                  {asset.name}
                </div>
                <div className="row" style={{ marginTop: 5, gap: 6 }}>
                  <StatusBadge status={asset.status} />
                  {validation && !validation.passed && (
                    <span className="badge fail" title={validation.failed_rules.join('、')}>
                      {validation.failed_rules.length} 项未过
                    </span>
                  )}
                </div>
                <div className="muted" style={{ marginTop: 5, fontSize: 12 }}>
                  变体 {counts.variants} · 版本 {counts.versions} · 导出 {counts.exports}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      <QueuePanel />
    </div>
  );
}

export default Library;

/**
 * 资产详情 —— 检查、挑选、校验、导出的主界面。
 *
 * 视口分层（5.3 关键交互决策）：前端只做检查与摆放，一切破坏性修改走后端管线。
 * 所以这里的每个"改网格"的动作，本质都是"提交一个任务 + 等版本树长出新节点"。
 */

import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, fileUrlSync } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import JobProgress from '../components/JobProgress';
import ValidationReport from '../components/ValidationReport';
import VersionTree from '../components/VersionTree';
import Viewport3D from '../components/Viewport3D';
import { StatusBadge } from '../components/StatusBadge';
import type { Locator, VersionNode } from '../api/types';

export function AssetDetail() {
  const { assetId = '' } = useParams();
  const navigate = useNavigate();
  const { detail, presets, job, openAsset, trackJob, clearJob, handle, settings } = useAppStore();

  const [activeVersionId, setActiveVersionId] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<number[]>([]);
  const [preset, setPreset] = useState('unity');
  const [allowFailedExport, setAllowFailedExport] = useState(false);
  const [exportResult, setExportResult] = useState<{ warnings: string[]; files: string[] } | null>(null);
  const [checking, setChecking] = useState(false);
  const [aiInstruction, setAiInstruction] = useState('');
  const [aiEditing, setAiEditing] = useState(false);
  const [aiEditResult, setAiEditResult] = useState<{ summary: string; applied: Record<string, unknown> } | null>(null);

  useEffect(() => {
    void openAsset(assetId);
  }, [assetId, openAsset]);

  const asset = detail?.asset;
  const versions = detail?.versions ?? [];
  const variants = detail?.variants ?? [];
  const reports = detail?.reports ?? [];

  const head = useMemo(
    () => versions.find((v) => v.id === asset?.head_version_id) ?? versions[versions.length - 1] ?? null,
    [versions, asset?.head_version_id],
  );

  // 默认看头节点；用户点了版本树就看他点的那一个
  useEffect(() => {
    if (!activeVersionId && head) setActiveVersionId(head.id);
  }, [activeVersionId, head]);

  const activeVersion: VersionNode | null =
    versions.find((v) => v.id === activeVersionId) ?? head ?? null;

  const activeReport = useMemo(
    () => reports.filter((r) => r.version_id === activeVersion?.id).slice(-1)[0] ?? null,
    [reports, activeVersion?.id],
  );

  const latestReport = reports[reports.length - 1] ?? null;
  const exportPresetList = Object.entries(presets?.export_presets ?? {});

  if (!asset) {
    return <div className="empty">加载中…</div>;
  }

  const pickedVariant = variants.find((v) => v.id === asset.picked_variant_id) ?? null;
  const needsPick = variants.length > 0 && !pickedVariant;

  const locate = (locator: Locator) => {
    setHighlight(locator.kind === 'none' ? [] : locator.indices);
  };

  const runPipeline = async () => {
    const started = await handle(() => api.runPipeline(asset.id));
    if (started) trackJob(started.job);
  };

  const regenerate = async () => {
    const started = await handle(() => api.generate(asset.id, 3));
    if (started) trackJob(started.job);
  };

  const pick = async (variantId: string) => {
    await handle(async () => {
      await api.pickVariant(asset.id, variantId);
      await openAsset(asset.id);
      return true;
    });
  };

  const rollback = async (version: VersionNode) => {
    await handle(async () => {
      await api.rollbackToVersion(asset.id, version.id);
      await openAsset(asset.id);
      // 回滚产生新的 head：让视口切过去看结果
      setActiveVersionId(null);
      setHighlight([]);
      return true;
    });
  };

  const runAiEdit = async () => {
    if (!aiInstruction.trim()) return;
    setAiEditing(true);
    try {
      const result = await api.aiEdit(asset.id, aiInstruction.trim());
      setAiEditResult({ summary: result.summary, applied: result.applied });
      setAiInstruction('');
      await openAsset(asset.id);
    } finally {
      setAiEditing(false);
    }
  };

  const runVisualCheck = async () => {
    setChecking(true);
    try {
      await handle(() => api.visualCheck(asset.id));
      await openAsset(asset.id);
    } finally {
      setChecking(false);
    }
  };

  const doExport = async () => {
    const result = await handle(() =>
      api.exportAsset(asset.id, preset, allowFailedExport),
    );
    if (result) {
      setExportResult({ warnings: result.warnings, files: result.export.files });
      await openAsset(asset.id);
    }
  };

  const archive = async () => {
    await handle(async () => {
      await api.archiveAsset(asset.id);
      navigate('/library');
      return true;
    });
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="row" style={{ gap: 8 }}>
            <button onClick={() => navigate('/library')}>← 资产库</button>
            <h1>{asset.name}</h1>
            <StatusBadge status={asset.status} />
          </div>
          <div className="sub">
            来源 {asset.source} · 规格「{asset.spec.name}」· {asset.spec.face_budget} 面 /{' '}
            {asset.spec.target_engine}
          </div>
        </div>
        <div className="row">
          {asset.kind === 'image' ? (
            <button
              className="primary"
              onClick={async () => {
                const started = await handle(() => api.generateImage(asset.id, asset.enhanced_prompt || asset.prompt));
                if (started) trackJob(started.job);
              }}
              disabled={!!job && job.status === 'running' || !asset.enhanced_prompt && !asset.prompt}
              title={!asset.enhanced_prompt && !asset.prompt ? '先在 AI 属性编辑里填写生成描述' : undefined}
            >
              重新生成图片（后台）
            </button>
          ) : (
            <>
              {asset.source === 'image' && (
                <button onClick={regenerate} disabled={!!job && job.status === 'running'}>
                  重新生成
                </button>
              )}
              <button
                className="primary"
                onClick={runPipeline}
                disabled={!!job && job.status === 'running' || (!pickedVariant && versions.length === 0)}
                title={!pickedVariant && versions.length === 0 ? '先挑选一个变体或导入网格' : undefined}
              >
                跑后处理管线
              </button>
            </>
          )}
          <button className="danger" onClick={archive}>
            归档
          </button>
        </div>
      </div>

      <JobProgress job={job} onCancel={() => job && api.cancelJob(job.id)} onDismiss={clearJob} />

      {needsPick && (
        <div className="banner info">
          已生成 {variants.length} 个变体，请先挑选一个再进后处理 —— 变体制的意义就是让美术挑，
          而不是接受唯一结果。
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 14 }}>
        <div>
          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>AI 属性编辑</h2>
              <span className="spacer" />
              {settings?.llm.configured ? null : (
                <span className="muted">需要先在 设置 → AI 助手 配置 Key</span>
              )}
            </div>
            <p className="muted" style={{ marginTop: 0 }}>
              用一句话让 AI 修改资产属性：改名、标签、备注、生成描述、面数预算、期望尺寸、贴图分辨率。
              改完立即生效（可在下方资产信息里核对）。
            </p>
            <div className="row" style={{ alignItems: 'flex-end' }}>
              <label style={{ flex: '1 1 320px' }}>
                <div className="muted">要改什么？</div>
                <input
                  value={aiInstruction}
                  onChange={(e) => setAiInstruction(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !aiEditing) void runAiEdit();
                  }}
                  placeholder="例如：改名为 SM_Iron_Chest，标签加上 金属、宝箱，面数预算降到 2000"
                />
              </label>
              <button
                className="primary"
                onClick={runAiEdit}
                disabled={aiEditing || !aiInstruction.trim() || !settings?.llm.configured}
              >
                {aiEditing ? 'AI 编辑中…' : '应用 AI 修改'}
              </button>
            </div>
            {aiEditResult && (
              <div style={{ marginTop: 10 }}>
                {aiEditResult.summary && <p style={{ margin: '4px 0' }}>{aiEditResult.summary}</p>}
                <pre
                  className="mono"
                  style={{
                    margin: 0,
                    background: 'var(--bg-subtle)',
                    padding: 10,
                    borderRadius: 8,
                    whiteSpace: 'pre-wrap',
                    fontSize: 12,
                  }}
                >
                  {JSON.stringify(aiEditResult.applied, null, 2)}
                </pre>
              </div>
            )}
          </div>

          {asset.kind === 'image' ? (
            <div className="card">
              <div className="row" style={{ marginBottom: 10 }}>
                <h2 style={{ margin: 0 }}>2D 素材预览</h2>
                <span className="spacer" />
                <span className="muted">
                  {detail?.thumbnail ? '最新生成图' : '还没有生成图'}
                </span>
              </div>
              {detail?.thumbnail ? (
                <img
                  src={fileUrlSync(detail.thumbnail)}
                  alt={asset.name}
                  style={{ width: '100%', borderRadius: 8, border: '0.5px solid var(--border)' }}
                />
              ) : (
                <div className="empty">还没有生成图片。点右上角「重新生成图片」或先填写描述。</div>
              )}
            </div>
          ) : (
          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>
                3D 检查
                {activeVersion && (
                  <span className="muted" style={{ fontWeight: 400 }}>
                    {' '}
                    · {activeVersion.op}
                    {activeVersion.id === head?.id ? '（当前）' : ''}
                  </span>
                )}
              </h2>
              <span className="spacer" />
              {highlight.length > 0 && (
                <button onClick={() => setHighlight([])}>清除高亮（{highlight.length} 个面）</button>
              )}
            </div>
            <Viewport3D
              url={activeVersion ? fileUrlSync(activeVersion.mesh_path) : null}
              highlight={highlight}
              emptyHint={asset.source === 'image' ? '先生成并挑选一个变体' : '导入网格后即可检查'}
            />
          </div>
          )}

          {variants.length > 0 && (
            <div className="card">
              <h2>变体（{variants.length}）</h2>
              <div className="variant-strip">
                {variants.map((variant) => (
                  <div
                    key={variant.id}
                    className={`variant-tile${variant.id === asset.picked_variant_id ? ' picked' : ''}`}
                    onClick={() => pick(variant.id)}
                    title={`${variant.provider} · ${variant.face_count ?? '?'} 面 · ¥${variant.cost}`}
                  >
                    {variant.thumbnail_path ? (
                      <img src={fileUrlSync(variant.thumbnail_path)} alt={variant.id} />
                    ) : (
                      <div className="muted" style={{ aspectRatio: 1, display: 'grid', placeItems: 'center' }}>
                        无预览
                      </div>
                    )}
                    <div className="mono" style={{ fontSize: 11, marginTop: 4 }}>
                      {variant.provider}
                    </div>
                    <div className="muted" style={{ fontSize: 11 }}>
                      {variant.face_count?.toLocaleString() ?? '?'} 面
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>AI 视觉校验</h2>
              <span className="spacer" />
              <button
                onClick={runVisualCheck}
                disabled={checking || (asset.kind === 'model' && !detail?.turntable?.length)}
                title={asset.kind === 'image' ? '视觉 LLM 对照资产描述检查图片' : '视觉 LLM 用概念图 + 转台帧对照描述打分'}
              >
                {checking ? '校验中…' : '开始 AI 视觉校验'}
              </button>
            </div>
            {settings?.llm.configured ? null : (
              <div className="banner warn" style={{ marginTop: 0, marginBottom: 10 }}>
                还没有配置 AI 助手（设置 → AI 助手），视觉校验不可用。
              </div>
            )}
            {(detail?.semantic_checks?.length ?? 0) > 0 ? (
              (() => {
                const check = detail!.semantic_checks![detail!.semantic_checks!.length - 1];
                return (
                  <div>
                    <div className="row" style={{ marginBottom: 6 }}>
                      <span className={`badge ${check.passed ? 'pass' : 'fail'}`}>
                        {check.passed ? '通过' : '未通过'} · {check.score} 分
                      </span>
                      <span className="muted">{new Date(check.created_at).toLocaleString()}</span>
                    </div>
                    <p style={{ marginTop: 0 }}>{check.summary}</p>
                    {check.issues.length > 0 && (
                      <ul style={{ margin: '6px 0', paddingLeft: 18 }}>
                        {check.issues.map((issue, i) => (
                          <li key={i} className="muted">
                            [{issue.severity}] {issue.message}
                          </li>
                        ))}
                      </ul>
                    )}
                    {check.suggestions.length > 0 && (
                      <p className="muted" style={{ marginBottom: 0 }}>
                        下次建议：{check.suggestions.join('；')}
                      </p>
                    )}
                  </div>
                );
              })()
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                还没有校验记录。视觉 LLM 会拿概念图（如有）和转台渲染帧对照资产描述，
                给出吻合度评分与问题清单。
              </p>
            )}
          </div>

          {(detail?.turntable?.length ?? 0) > 0 && (
            <div className="card">
              <h2>转台（{detail!.turntable.length} 帧）</h2>
              <p className="muted" style={{ marginTop: 0 }}>
                管线完成后的 8 帧环绕图，用于快速检查全角度形态与贴图接缝。
              </p>
              <div className="variant-strip">
                {detail!.turntable.map((frame, index) => (
                  <img
                    key={frame}
                    src={fileUrlSync(frame)}
                    alt={`转台帧 ${index}`}
                    style={{ width: 110, borderRadius: 8, border: '0.5px solid var(--border)' }}
                  />
                ))}
              </div>
            </div>
          )}

          {asset.kind === 'model' && (
          <div className="card">
            <div className="row" style={{ marginBottom: 10 }}>
              <h2 style={{ margin: 0 }}>
                校验报告 {activeReport ? (activeReport.passed ? '· 通过' : '· 未通过') : ''}
              </h2>
              <span className="spacer" />
              {activeReport && activeReport.id !== latestReport?.id && (
                <span className="muted">当前显示的是该版本的报告</span>
              )}
            </div>
            <ValidationReport report={activeReport} onLocate={locate} />
          </div>
          )}

          {asset.kind === 'model' && (
          <div className="card">
            <h2>导出</h2>
            <div className="row wrap" style={{ alignItems: 'flex-end' }}>
              <label style={{ flex: '0 0 240px' }}>
                <div className="muted">引擎预设</div>
                <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                  {exportPresetList.map(([key, info]) => (
                    <option key={key} value={key}>
                      {info.display_name}
                      {info.up_axis ? `（${info.up_axis}-up / ${info.unit}）` : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="row" style={{ gap: 6, flex: '0 0 auto' }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={allowFailedExport}
                  onChange={(e) => setAllowFailedExport(e.target.checked)}
                />
                <span className="muted">忽略校验结果继续导出</span>
              </label>
              <button className="primary" onClick={doExport} disabled={!activeVersion}>
                导出
              </button>
            </div>

            {activeReport && !activeReport.passed && !allowFailedExport && (
              <div className="banner warn" style={{ marginTop: 12, marginBottom: 0 }}>
                校验未通过，导出会被拦截。你可以先「跑后处理管线」自动修复，
                或勾选上面的「忽略校验结果继续导出」。
              </div>
            )}

            {exportResult && (
              <div style={{ marginTop: 12 }}>
                <div className="row wrap">
                  <span className="badge pass">已导出 {exportResult.files.length} 个文件</span>
                  <button onClick={() => window.assetagent?.reveal(exportResult.files[0])}>
                    在文件夹中显示
                  </button>
                </div>
                {exportResult.warnings.map((warning) => (
                  <div key={warning} className="banner warn" style={{ marginTop: 8, marginBottom: 0 }}>
                    {warning}
                  </div>
                ))}
              </div>
            )}

            {(detail?.exports?.length ?? 0) > 0 && (
              <div style={{ marginTop: 12 }}>
                <h3>历史导出</h3>
                <table className="rules">
                  <tbody>
                    {detail!.exports.slice(-5).reverse().map((record) => (
                      <tr key={record.id}>
                        <td className="mono" style={{ width: 90 }}>{record.preset}</td>
                        <td className="mono">{record.files.length} 个文件</td>
                        <td className="muted">{new Date(record.created_at).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          )}
        </div>

        <div>
          <div className="card">
            <h2>版本树</h2>
            <VersionTree
              versions={versions}
              activeVersionId={activeVersion?.id ?? null}
              headVersionId={asset.head_version_id}
              onSelect={(version) => {
                setActiveVersionId(version.id);
                setHighlight([]);
              }}
              onRollback={rollback}
            />
          </div>

          <div className="card">
            <h2>资产信息</h2>
            <dl className="kv">
              <dt>ID</dt>
              <dd className="mono">{asset.id}</dd>
              <dt>规格</dt>
              <dd>{asset.spec.name}</dd>
              <dt>面数预算</dt>
              <dd>{asset.spec.face_budget}</dd>
              <dt>目标引擎</dt>
              <dd>{asset.spec.target_engine}</dd>
              <dt>期望尺寸</dt>
              <dd>{asset.spec.expected_size_m ? `${asset.spec.expected_size_m} m` : '未指定'}</dd>
              <dt>贴图分辨率</dt>
              <dd>{asset.spec.texture_resolution}</dd>
              <dt>命名规范</dt>
              <dd className="mono">{asset.spec.naming_pattern}</dd>
              <dt>源文件</dt>
              <dd className="mono" style={{ wordBreak: 'break-all' }}>
                {asset.source_files.length} 个（只读）
              </dd>
            </dl>
            {asset.source_files.length > 0 && (
              <button
                style={{ marginTop: 10 }}
                onClick={() => window.assetagent?.reveal(asset.source_files[0])}
              >
                在文件夹中显示源文件
              </button>
            )}
          </div>

          {detail && detail.jobs.length > 0 && (
            <div className="card">
              <h2>任务记录</h2>
              <table className="rules">
                <tbody>
                  {detail.jobs.slice(-6).reverse().map((item) => (
                    <tr key={item.id}>
                      <td className="mono" style={{ width: 80 }}>{item.step}</td>
                      <td>
                        <span className={`badge ${item.status}`}>{item.status}</span>
                      </td>
                      <td className="muted" style={{ fontSize: 12 }}>
                        {item.error ?? item.message ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {!window.assetagent && (
            <div className="banner warn">
              当前在浏览器里运行，无法调用系统能力（如在文件夹中显示）。请用桌面端启动。
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default AssetDetail;

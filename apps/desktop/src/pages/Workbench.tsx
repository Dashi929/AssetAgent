/**
 * 工作台 —— 两个入口：
 *
 * - **新建**：文字或图片出发，用 LLM 优化提示词后生成全新的 2D 图片或 3D 模型素材；
 * - **导入**：拖入 2D/3D 素材文件，后台队列自动处理（模型自动跑整条管线），
 *   完成后通知并引导跳转到预览/编辑模式。
 *
 * 两条入口都会在资产库生成资产；资产详情页是预览/编辑模式。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import JobProgress from '../components/JobProgress';

type Mode = 'create' | 'import';
type OutputKind = 'model' | 'image';

const ONBOARDING_KEY = 'assetagent.onboarding.dismissed';

interface ImportRow {
  filename: string;
  jobId: string;
  assetId?: string;
  assetName?: string;
  status: 'pending' | 'succeeded' | 'failed';
  kind?: string;
}

export function Workbench() {
  const navigate = useNavigate();
  const { presets, settings, job, trackJob, clearJob, handle, setError, refreshAssets, assets } =
    useAppStore();

  const [mode, setMode] = useState<Mode>('create');
  const [outputKind, setOutputKind] = useState<OutputKind>('model');
  const [dragging, setDragging] = useState(false);
  const [name, setName] = useState('');
  const [presetKey, setPresetKey] = useState('');
  const [prompt, setPrompt] = useState('');
  const [variants, setVariants] = useState(3);
  const [provider, setProvider] = useState('');
  const [pickedFiles, setPickedFiles] = useState<File[]>([]);
  const [estimateCny, setEstimateCny] = useState<number | null>(null);
  const [enhancing, setEnhancing] = useState(false);
  const [enhanceInfo, setEnhanceInfo] = useState('');
  const [estimateProvider, setEstimateProvider] = useState('');
  const [importRows, setImportRows] = useState<ImportRow[]>([]);
  const [importing, setImporting] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(
    () => localStorage.getItem(ONBOARDING_KEY) !== '1',
  );
  const inputRef = useRef<HTMLInputElement>(null);

  const isImageOutput = outputKind === 'image';

  // 精确预估：走 Provider 自己的算法（离线占位/本地模型报 ¥0，不报假价格）。
  // 静默失败（sidecar 未就绪等）不弹全局错误，只把预估留空。
  useEffect(() => {
    if (mode !== 'create' || isImageOutput) {
      setEstimateCny(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const result = await api.getEstimate(provider, variants);
        if (!cancelled) {
          setEstimateCny(result.estimate_cny);
          setEstimateProvider(result.provider);
        }
      } catch {
        if (!cancelled) setEstimateCny(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, isImageOutput, provider, variants]);

  const presetList = useMemo(() => Object.entries(presets?.spec_presets ?? {}), [presets]);
  const activePresetKey = presetKey || presetList[0]?.[0] || '';
  const activePreset = presets?.spec_presets?.[activePresetKey];

  const providerList = useMemo(
    () => (settings?.providers ?? []).filter((p) => p.name !== 'local_trellis'),
    [settings],
  );
  const readyProviders = providerList.filter((p) => p.has_key);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDragging(false);
      const files = Array.from(event.dataTransfer.files);
      if (files.length === 0) return;
      // 导入模式支持 2D/3D 混合多选；新建只收概念图
      setPickedFiles(mode === 'create' ? files : files);
    },
    [mode],
  );

  const openFileDialog = () => inputRef.current?.click();

  const onInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    if (files.length) setPickedFiles(files);
    // 清空 value，否则同一个文件第二次选不会触发 change
    event.target.value = '';
  };

  const accept =
    mode === 'create'
      ? isImageOutput
        ? 'image/*'
        : 'image/*'
      : '.glb,.gltf,.obj,.fbx,.ply,.stl,.png,.jpg,.jpeg,.webp,.bmp';

  // ---------------------------------------------------- 新建（2D / 3D）

  const submitCreate = async () => {
    const assetName = name.trim() || (isImageOutput ? 'SM_New_Image' : 'SM_New_Prop');

    const created = await handle(async () => {
      if (isImageOutput) {
        // 2D：纯文字描述（概念图可选作为附件参考），生成在后台任务里跑
        return api.createAsset({
          name: assetName,
          kind: 'image',
          source: 'image',
          prompt: prompt.trim(),
        });
      }
      if (pickedFiles.length === 0) {
        throw new ApiError('请先拖入一张概念图，或切到纯文字模式（见下方按钮）。', 400);
      }
      return api.createAssetFromImages(pickedFiles, assetName, activePresetKey, prompt);
    });
    if (!created) return;

    await refreshAssets();

    if (isImageOutput) {
      const started = await handle(() => api.generateImage(created.asset.id, prompt.trim()));
      if (started) trackJob(started.job);
      navigate(`/asset/${created.asset.id}`);
      return;
    }

    const started = await handle(() =>
      api.generate(created.asset.id, variants, provider || undefined, prompt),
    );
    if (started) trackJob(started.job);
    navigate(`/asset/${created.asset.id}`);
  };

  // ---------------------------------------------------- 导入（后台队列）

  const startImport = async () => {
    if (pickedFiles.length === 0) {
      setError('请先拖入要导入的 2D/3D 素材文件。');
      return;
    }
    setImporting(true);
    try {
      const result = await api.importAssets(pickedFiles, name.trim());
      await refreshAssets();
      const rows: ImportRow[] = result.imports.map((item) => ({
        filename: item.filename ?? item.asset?.asset.name ?? '未命名',
        jobId: item.job?.id ?? '',
        assetId: item.asset?.asset.id,
        assetName: item.asset?.asset.name,
        status: item.kind === 'unsupported' ? 'failed' : 'pending',
        kind: item.kind,
      }));
      setImportRows((prev) => [...prev, ...rows]);
      setPickedFiles([]);
    } catch (error) {
      setError(error instanceof ApiError ? error.message : '导入失败，请稍后重试。');
    } finally {
      setImporting(false);
    }
  };

  // 导入任务的轮询：所有行都到终态后停止，并刷新资产库
  useEffect(() => {
    const pending = importRows.filter((r) => r.status === 'pending' && r.jobId);
    if (pending.length === 0) return;
    const timer = setInterval(async () => {
      let changed = false;
      const next = await Promise.all(
        importRows.map(async (row) => {
          if (row.status !== 'pending' || !row.jobId) return row;
          try {
            const j = await api.getJob(row.jobId);
            if (j.status === 'succeeded') {
              changed = true;
              return { ...row, status: 'succeeded' as const };
            }
            if (j.status === 'failed' || j.status === 'cancelled') {
              changed = true;
              return { ...row, status: 'failed' as const };
            }
            return row;
          } catch {
            return row;
          }
        }),
      );
      if (changed) {
        setImportRows(next);
        await refreshAssets();
      }
    }, 1200);
    return () => clearInterval(timer);
  }, [importRows, refreshAssets]);

  const pendingImports = importRows.filter((r) => r.status === 'pending').length;
  const doneImports = importRows.filter((r) => r.status === 'succeeded').length;

  const submit = async () => {
    if (mode === 'import') return startImport();
    return submitCreate();
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>工作台</h1>
          <div className="sub">新建 AI 素材，或导入已有素材进入预览/编辑</div>
        </div>
        <div className="row">
          <button className={mode === 'create' ? 'primary' : ''} onClick={() => setMode('create')}>
            新建
          </button>
          <button className={mode === 'import' ? 'primary' : ''} onClick={() => setMode('import')}>
            导入
          </button>
        </div>
      </div>

      <JobProgress
        job={job}
        onCancel={() => job && api.cancelJob(job.id)}
        onDismiss={clearJob}
      />

      {showOnboarding && assets.length === 0 && (
        <div className="banner info">
          <strong>两种玩法：</strong>「新建」用一句描述或一张概念图，AI 生成全新素材；「导入」
          拖入已有 2D/3D 文件，后台自动处理。两者都在资产库生成资产，点开即进入预览/编辑。&nbsp;
          <button
            onClick={() => {
              localStorage.setItem(ONBOARDING_KEY, '1');
              setShowOnboarding(false);
            }}
          >
            知道了
          </button>
        </div>
      )}

      {mode === 'create' && (
        <>
          <div className="card">
            <div
              className={`dropzone${dragging ? ' over' : ''}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={openFileDialog}
              style={{ cursor: 'pointer' }}
            >
              <div style={{ fontSize: 14 }}>
                {isImageOutput ? '（可选）拖入参考图' : '拖入概念图，或点击选择'}
              </div>
              <div className="hint">
                {isImageOutput
                  ? '参考图会作为附件保存；生成主要依据下方描述'
                  : '支持 png / jpg / webp，可多选（主视图 + 正/侧视图能明显提升质量）；纯文字描述也可以，见下方按钮'}
              </div>
              {pickedFiles.length > 0 && (
                <div style={{ marginTop: 10 }} className="mono">
                  已选 {pickedFiles.length} 个文件：{pickedFiles.map((f) => f.name).join('、')}
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                hidden
                multiple
                accept={accept}
                onChange={onInputChange}
              />
            </div>
          </div>

          <div className="card">
            <h2>规格与参数</h2>
            <div className="row wrap" style={{ alignItems: 'flex-end', gap: 14 }}>
              <label style={{ flex: '0 0 180px' }}>
                <div className="muted">素材类型</div>
                <select
                  value={outputKind}
                  onChange={(e) => setOutputKind(e.target.value as OutputKind)}
                >
                  <option value="model">3D 模型</option>
                  <option value="image">2D 图片</option>
                </select>
              </label>

              <label style={{ flex: '1 1 200px' }}>
                <div className="muted">资产名（决定导出文件名，需符合引擎命名规范）</div>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={isImageOutput ? 'SM_New_Image' : 'SM_New_Prop'}
                />
              </label>

              {!isImageOutput && (
                <>
                  <label style={{ flex: '1 1 220px' }}>
                    <div className="muted">规格预设</div>
                    <select value={activePresetKey} onChange={(e) => setPresetKey(e.target.value)}>
                      {presetList.map(([key, preset]) => (
                        <option key={key} value={key}>
                          {preset.name}（{preset.face_budget} 面 / {preset.target_engine}）
                        </option>
                      ))}
                    </select>
                  </label>

                  <label style={{ flex: '0 0 110px' }}>
                    <div className="muted">变体数量</div>
                    <select value={variants} onChange={(e) => setVariants(Number(e.target.value))}>
                      {[1, 2, 3, 4].map((n) => (
                        <option key={n} value={n}>
                          {n} 个
                        </option>
                      ))}
                    </select>
                  </label>

                  <label style={{ flex: '1 1 180px' }}>
                    <div className="muted">生成引擎</div>
                    <select value={provider} onChange={(e) => setProvider(e.target.value)}>
                      <option value="">自动（优先已配置 Key 的）</option>
                      {providerList.map((p) => (
                        <option key={p.name} value={p.name} disabled={!p.has_key && p.name !== 'mock'}>
                          {p.display_name}
                          {p.has_key ? '' : '（未配置 Key）'}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              )}
            </div>

            <label style={{ display: 'block', marginTop: 12 }}>
              <div className="muted">
                素材描述{isImageOutput ? '（必填：AI 生成图片的依据）' : '（可选，图为主文字为辅）'}
                <button
                  style={{ marginLeft: 8, padding: '1px 8px', fontSize: 12 }}
                  disabled={enhancing || !prompt.trim()}
                  title={prompt.trim() ? 'AI 按知识库优化这段描述' : '先输入一句描述'}
                  onClick={async () => {
                    setEnhancing(true);
                    try {
                      const result = isImageOutput
                        ? await api.enhancePromptPreview(prompt.trim(), '')
                        : await api.enhancePromptPreview(prompt.trim(), activePresetKey);
                      setPrompt(result.prompt);
                      setEnhanceInfo(result.keywords.length ? `关键词：${result.keywords.join('、')}` : result.rationale);
                    } catch (error) {
                      setEnhanceInfo('');
                      setError(error instanceof ApiError ? error.message : 'AI 优化失败，请稍后重试。');
                    } finally {
                      setEnhancing(false);
                    }
                  }}
                >
                  {enhancing ? '优化中…' : 'AI 优化描述'}
                </button>
              </div>
              <input
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={
                  isImageOutput
                    ? '例如：像素风格的红药水图标，纯色背景'
                    : '例如：写实风格、金属磨损质感'
                }
              />
              {enhanceInfo && <div className="muted" style={{ marginTop: 4 }}>{enhanceInfo}</div>}
            </label>

            {!isImageOutput && activePreset && (
              <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
                交付契约：≤ {activePreset.face_budget} 三角面 · 轴心{activePreset.pivot === 'bottom_center' ? '底面中心' : activePreset.pivot}
                {activePreset.expected_size_m ? ` · 最长边约 ${activePreset.expected_size_m}m` : ''} ·{' '}
                {activePreset.texture_resolution} 贴图 · 命名 {activePreset.naming_pattern}
              </p>
            )}

            {!isImageOutput && readyProviders.length === 0 && (
              <div className="banner warn" style={{ marginTop: 12, marginBottom: 0 }}>
                还没有配置任何生成引擎的 API Key。当前会走**离线占位模式**（本地生成占位网格），
                可以完整验证管线与校验器，但产出的不是真实资产。
                到「设置 → BYOK」填写 Key 即可切换到真实生成。
              </div>
            )}
            {isImageOutput && !settings?.llm.configured && (
              <div className="banner warn" style={{ marginTop: 12, marginBottom: 0 }}>
                2D 图片生成需要在「设置 → AI 助手」里配置 API Key（默认智谱开放平台，
                与提示词优化/视觉校验共用同一把 Key）。
              </div>
            )}

            <div className="row" style={{ marginTop: 14 }}>
              <button
                className="primary"
                onClick={submit}
                disabled={isImageOutput && !prompt.trim()}
                title={isImageOutput && !prompt.trim() ? '2D 生成需要一句描述' : undefined}
              >
                {isImageOutput ? '生成图片（后台）' : '开始生成'}
              </button>
              {!isImageOutput && estimateCny !== null && (
                <span className="muted">
                  {estimateCny > 0
                    ? `本次预估花费 ¥${estimateCny.toFixed(2)}（${estimateProvider}）`
                    : `本次免费（将走 ${estimateProvider || '离线占位'} 模式）`}
                </span>
              )}
              {isImageOutput && <span className="muted">生成任务在后台队列执行，完成后在资产详情页查看</span>}
            </div>
          </div>
        </>
      )}

      {mode === 'import' && (
        <>
          <div className="card">
            <div
              className={`dropzone${dragging ? ' over' : ''}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
              onClick={openFileDialog}
              style={{ cursor: 'pointer' }}
            >
              <div style={{ fontSize: 14 }}>拖入 2D / 3D 素材文件，或点击选择（可多选混合）</div>
              <div className="hint">
                3D：glb / gltf / obj / fbx / ply / stl —— 导入后自动跑完整后处理管线；
                2D：png / jpg / webp / bmp —— 导入后即可用 AI 编辑属性
              </div>
              {pickedFiles.length > 0 && (
                <div style={{ marginTop: 10 }} className="mono">
                  已选 {pickedFiles.length} 个文件：{pickedFiles.map((f) => f.name).join('、')}
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                hidden
                multiple
                accept={accept}
                onChange={onInputChange}
              />
            </div>
            <div className="row" style={{ marginTop: 14 }}>
              <label style={{ flex: '1 1 240px' }}>
                <div className="muted">
                  资产名（仅单文件导入时使用；多文件自动用文件名）
                </div>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="SM_Imported_Prop"
                />
              </label>
              <label style={{ flex: '1 1 220px' }}>
                <div className="muted">规格预设（仅对 3D 网格生效）</div>
                <select value={activePresetKey} onChange={(e) => setPresetKey(e.target.value)}>
                  {presetList.map(([key, preset]) => (
                    <option key={key} value={key}>
                      {preset.name}（{preset.face_budget} 面 / {preset.target_engine}）
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="primary"
                onClick={startImport}
                disabled={importing || pickedFiles.length === 0}
              >
                {importing ? '导入中…' : `开始导入${pickedFiles.length ? `（${pickedFiles.length} 个文件）` : ''}`}
              </button>
            </div>
            <p className="muted" style={{ marginTop: 10, marginBottom: 0 }}>
              导入在后台队列执行（模型会自动跑完整管线），可以随时切到其它页面，完成后这里会通知。
            </p>
          </div>

          {importRows.length > 0 && (
            <div className="card">
              <h2>导入进度</h2>
              {doneImports > 0 && pendingImports === 0 && (
                <div className="banner info" style={{ marginTop: 0 }}>
                  ✅ 全部导入完成（{doneImports} 个）—— 点击下方「查看」进入预览/编辑模式。
                </div>
              )}
              <table className="rules" style={{ width: '100%' }}>
                <tbody>
                  {importRows.map((row, index) => (
                    <tr key={`${row.jobId}-${index}`}>
                      <td className="mono" style={{ width: '40%' }}>{row.filename}</td>
                      <td>
                        {row.status === 'pending' && <span className="badge">后台处理中…</span>}
                        {row.status === 'succeeded' && <span className="badge pass">导入完成</span>}
                        {row.status === 'failed' && <span className="badge fail">失败</span>}
                      </td>
                      <td style={{ textAlign: 'right' }}>
                        {row.status === 'succeeded' && row.assetId && (
                          <button className="primary" onClick={() => navigate(`/asset/${row.assetId}`)}>
                            查看 / 编辑
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {pendingImports > 0 && (
                <p className="muted" style={{ marginBottom: 0 }}>
                  还有 {pendingImports} 个文件在后台队列处理中（大型模型可能需要一两分钟）。
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default Workbench;

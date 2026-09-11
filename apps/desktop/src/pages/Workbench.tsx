/**
 * 工作台 —— 创建入口。
 *
 * 两条入口对应两条工作流：
 * - 拖概念图 → 工作流 A（生成 + 后处理），需要 API Key
 * - 拖已有网格 → 工作流 C（纯后处理），零成本，也是生成失败时的兜底
 *
 * 设计原则 1.3：**美术是"挑选者"，不是 prompt 工程师**。所以这里输入以图为主、
 * 文字为辅，且默认生成多个变体让美术挑，而不是接受唯一结果。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import JobProgress from '../components/JobProgress';

type Mode = 'concept' | 'mesh';

const ONBOARDING_KEY = 'assetagent.onboarding.dismissed';

export function Workbench() {
  const navigate = useNavigate();
  const { presets, settings, job, trackJob, clearJob, handle, setError, refreshAssets, assets } =
    useAppStore();

  const [mode, setMode] = useState<Mode>('concept');
  const [dragging, setDragging] = useState(false);
  const [name, setName] = useState('');
  const [presetKey, setPresetKey] = useState('');
  const [prompt, setPrompt] = useState('');
  const [variants, setVariants] = useState(3);
  const [provider, setProvider] = useState('');
  const [pickedFiles, setPickedFiles] = useState<File[]>([]);
  const [estimateCny, setEstimateCny] = useState<number | null>(null);
  const [estimateProvider, setEstimateProvider] = useState('');
  const [showOnboarding, setShowOnboarding] = useState(
    () => localStorage.getItem(ONBOARDING_KEY) !== '1',
  );
  const inputRef = useRef<HTMLInputElement>(null);

  // 精确预估：走 Provider 自己的算法（离线占位/本地模型报 ¥0，不报假价格）。
  // 静默失败（sidecar 未就绪等）不弹全局错误，只把预估留空。
  useEffect(() => {
    if (mode !== 'concept') {
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
  }, [mode, provider, variants]);

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
      if (mode === 'concept') {
        setPickedFiles(files);
      } else {
        setPickedFiles(files.slice(0, 1));
      }
    },
    [mode],
  );

  const openFileDialog = () => inputRef.current?.click();

  const onInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    if (files.length) setPickedFiles(mode === 'concept' ? files : files.slice(0, 1));
    // 清空 value，否则同一个文件第二次选不会触发 change
    event.target.value = '';
  };

  const submit = async () => {
    if (pickedFiles.length === 0) {
      setError(mode === 'concept' ? '请先拖入一张概念图。' : '请先拖入一个已有网格。');
      return;
    }
    const assetName = name.trim() || (mode === 'concept' ? 'SM_New_Prop' : 'SM_Imported_Prop');

    // 闪退/报错时的最小 breadcrumb：导入开始 → 成功/失败 → 管线启动
    const breadcrumb = (message: string) => window.assetagent?.appendLog(`[workbench] ${message}`);
    if (mode === 'mesh') {
      const f = pickedFiles[0];
      breadcrumb(`导入开始 file=${f.name} size=${f.size}B`);
    }

    const created = await handle(async () => {
      if (mode === 'concept') {
        return api.createAssetFromImages(pickedFiles, assetName, activePresetKey, prompt);
      }
      return api.createAssetFromMesh(pickedFiles[0], assetName, activePresetKey);
    });
    if (!created) {
      breadcrumb('导入失败（原因见上方错误提示与 sidecar.log）');
      return;
    }
    breadcrumb(`导入完成 asset=${created.asset.id}`);

    await refreshAssets();

    if (mode === 'mesh') {
      // 工作流 C：直接进管线，不碰生成环节
      breadcrumb(`管线启动 asset=${created.asset.id}`);
      const started = await handle(() => api.runPipeline(created.asset.id));
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

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>工作台</h1>
          <div className="sub">把概念图变成能直接拖进 Unity / UE 的资产</div>
        </div>
        <div className="row">
          <button className={mode === 'concept' ? 'primary' : ''} onClick={() => setMode('concept')}>
            概念图 → 资产
          </button>
          <button className={mode === 'mesh' ? 'primary' : ''} onClick={() => setMode('mesh')}>
            已有网格 → 后处理
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
          <strong>三步出资产：</strong>① 把概念图拖进下方区域（没有图？直接拖 FBX/GLB 走免费后处理）
          → ② 从生成的变体里挑一个 → ③ 跑管线、看校验、导出到引擎。
          没配 API Key 也能完整走通（生成走离线占位模式）。&nbsp;
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
            {mode === 'concept' ? '拖入概念图，或点击选择' : '拖入已有网格，或点击选择'}
          </div>
          <div className="hint">
            {mode === 'concept'
              ? '支持 png / jpg / webp，可多选（主视图 + 正/侧视图能明显提升质量）'
              : '支持 glb / gltf / obj / fbx / ply / stl，自动转为 GLB 工作副本'}
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
            multiple={mode === 'concept'}
            accept={mode === 'concept' ? 'image/*' : '.glb,.gltf,.obj,.fbx,.ply,.stl'}
            onChange={onInputChange}
          />
        </div>
      </div>

      <div className="card">
        <h2>规格与参数</h2>
        <div className="row wrap" style={{ alignItems: 'flex-end', gap: 14 }}>
          <label style={{ flex: '1 1 200px' }}>
            <div className="muted">资产名（决定导出文件名，需符合引擎命名规范）</div>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={mode === 'concept' ? 'SM_New_Prop' : 'SM_Imported_Prop'}
            />
          </label>

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

          {mode === 'concept' && (
            <>
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

        {mode === 'concept' && (
          <label style={{ display: 'block', marginTop: 12 }}>
            <div className="muted">补充描述（可选，图为主文字为辅）</div>
            <input
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="例如：写实风格、金属磨损质感"
            />
          </label>
        )}

        {activePreset && (
          <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
            交付契约：≤ {activePreset.face_budget} 三角面 · 轴心{activePreset.pivot === 'bottom_center' ? '底面中心' : activePreset.pivot}
            {activePreset.expected_size_m ? ` · 最长边约 ${activePreset.expected_size_m}m` : ''} ·{' '}
            {activePreset.texture_resolution} 贴图 · 命名 {activePreset.naming_pattern}
          </p>
        )}

        {mode === 'concept' && readyProviders.length === 0 && (
          <div className="banner warn" style={{ marginTop: 12, marginBottom: 0 }}>
            还没有配置任何生成引擎的 API Key。当前会走**离线占位模式**（本地生成占位网格），
            可以完整验证管线与校验器，但产出的不是真实资产。
            到「设置 → BYOK」填写 Key 即可切换到真实生成。
          </div>
        )}

        <div className="row" style={{ marginTop: 14 }}>
          <button className="primary" onClick={submit}>
            {mode === 'concept' ? '开始生成' : '开始后处理'}
          </button>
          {mode === 'concept' && estimateCny !== null && (
            <span className="muted">
              {estimateCny > 0
                ? `本次预估花费 ¥${estimateCny.toFixed(2)}（${estimateProvider}）`
                : `本次免费（将走 ${estimateProvider || '离线占位'} 模式）`}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default Workbench;

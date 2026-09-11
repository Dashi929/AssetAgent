/**
 * 设置页。
 *
 * BYOK 是这个页面的主角：自填 Key 只写本机（sidecar 侧 data/secrets.json，已 gitignore），
 * 界面永远只回显掩码。中转（relay）在 Phase 2，这里只保留路由开关本身。
 */

import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import type { Diagnostics } from '../api/types';

export function Settings() {
  const { sidecar, settings, refreshSettings, handle } = useAppStore();
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [testResults, setTestResults] = useState<Record<string, string>>({});
  const [log, setLog] = useState('');
  const [budget, setBudget] = useState('');
  const [cost, setCost] = useState('');
  const [faceBudget, setFaceBudget] = useState('');
  const [blender, setBlender] = useState('');

  useEffect(() => {
    void (async () => {
      const data = await handle(() => api.getDiagnostics());
      if (data) setDiagnostics(data);
    })();
  }, [handle]);

  useEffect(() => {
    if (!settings) return;
    setBudget(String(settings.monthly_budget_cny));
    setCost(String(settings.cost_per_generation_cny));
    setFaceBudget(String(settings.default_face_budget));
    setBlender(settings.blender_bin);
  }, [settings]);

  const providers = (settings?.providers ?? []).filter((p) => p.name !== 'local_trellis');

  const saveKey = async (provider: string) => {
    const draft = (keyDrafts[provider] ?? '').trim();
    if (!draft) return;
    await handle(async () => {
      await api.setByokKey(provider, draft);
      await refreshSettings();
      setKeyDrafts((prev) => ({ ...prev, [provider]: '' }));
      return true;
    });
  };

  const clearKey = async (provider: string) => {
    await handle(async () => {
      await api.clearByokKey(provider);
      await refreshSettings();
      return true;
    });
  };

  const testKey = async (provider: string) => {
    const result = await handle(() => api.testProvider(provider));
    setTestResults((prev) => ({
      ...prev,
      [provider]: result ? `${result.ok ? '✅' : '❌'} ${result.message}` : '❌ 测试请求失败',
    }));
  };

  const saveNumbers = async () => {
    await handle(async () => {
      await api.patchSettings({
        monthly_budget_cny: Number(budget) || 0,
        cost_per_generation_cny: Number(cost) || 0,
        default_face_budget: Number(faceBudget) || 5000,
        blender_bin: blender,
      });
      await refreshSettings();
      return true;
    });
  };

  const toggleRoute = async (mode: 'byok' | 'relay') => {
    await handle(async () => {
      await api.patchSettings({ route_mode: mode });
      await refreshSettings();
      return true;
    });
  };

  const loadLog = async () => {
    const bridge = window.assetagent;
    setLog(bridge ? await bridge.getSidecarLog() : '（浏览器环境，没有 sidecar 日志）');
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>设置</h1>
          <div className="sub">生成引擎、预算、依赖诊断</div>
        </div>
      </div>

      <div className="card">
        <h2>本地服务</h2>
        <dl className="kv">
          <dt>状态</dt>
          <dd>
            {sidecar ? (
              <span className={`badge ${sidecar.state === 'ready' ? 'pass' : 'fail'}`}>
                {sidecar.state === 'ready' ? '运行中' : sidecar.state}
              </span>
            ) : (
              <span className="badge">浏览器环境</span>
            )}
          </dd>
          <dt>地址</dt>
          <dd className="mono">{sidecar?.baseUrl || diagnostics?.data_dir || '—'}</dd>
          <dt>Python</dt>
          <dd className="mono">{sidecar?.pythonPath ?? diagnostics?.python ?? '—'}</dd>
          <dt>数据目录</dt>
          <dd className="mono" style={{ wordBreak: 'break-all' }}>{settings?.data_dir ?? '—'}</dd>
        </dl>
        {sidecar?.state === 'failed' && (
          <div className="banner error" style={{ marginTop: 10 }}>
            {sidecar.message}
            {sidecar.logTail && <pre className="mono" style={{ margin: '8px 0 0', whiteSpace: 'pre-wrap' }}>{sidecar.logTail}</pre>}
          </div>
        )}
        <div className="row" style={{ marginTop: 10 }}>
          <button
            onClick={async () => {
              const bridge = window.assetagent;
              if (bridge) {
                await bridge.restartSidecar();
                await refreshSettings();
              }
            }}
          >
            重启本地服务
          </button>
          <button onClick={loadLog}>查看日志</button>
        </div>
        {log && (
          <pre
            className="mono"
            style={{
              marginTop: 10,
              maxHeight: 220,
              overflow: 'auto',
              background: 'var(--bg-subtle)',
              padding: 10,
              borderRadius: 8,
              whiteSpace: 'pre-wrap',
            }}
          >
            {log}
          </pre>
        )}
      </div>

      <div className="card">
        <h2>生成路由</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          已订阅走官方中转，未订阅走自填 Key（BYOK）。中转服务在 Phase 2 上线，当前请使用 BYOK。
        </p>
        <div className="row">
          <button className={settings?.route_mode === 'byok' ? 'primary' : ''} onClick={() => toggleRoute('byok')}>
            BYOK（自填 Key）
          </button>
          <button className={settings?.route_mode === 'relay' ? 'primary' : ''} onClick={() => toggleRoute('relay')}>
            官方中转
          </button>
        </div>
      </div>

      <div className="card">
        <h2>生成引擎 API Key（BYOK）</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Key 只存本机、永不上传。不填任何 Key 时，生成会走离线占位模式，后处理与校验仍然完整可用。
        </p>

        {providers.map((provider) => (
          <div key={provider.name} style={{ borderTop: '0.5px solid var(--border)', paddingTop: 12, marginTop: 12 }}>
            <div className="row">
              <strong>{provider.display_name}</strong>
              {provider.has_key ? (
                <span className="badge pass">已配置 · {provider.key_masked}</span>
              ) : (
                <span className="badge">未配置</span>
              )}
              {provider.mode === 'mock' && <span className="badge">离线占位</span>}
              <span className="spacer" />
              <span className="muted mono">{provider.capabilities.join(' / ')}</span>
            </div>
            {provider.note && <p className="muted" style={{ margin: '6px 0' }}>{provider.note}</p>}

            {provider.name !== 'mock' && (
              <>
                <div className="row" style={{ marginTop: 8 }}>
                  <input
                    type="password"
                    placeholder={provider.has_key ? '输入新 Key 可覆盖（留空则不变）' : '粘贴 API Key'}
                    value={keyDrafts[provider.name] ?? ''}
                    onChange={(e) =>
                      setKeyDrafts((prev) => ({ ...prev, [provider.name]: e.target.value }))
                    }
                  />
                  <button className="primary" onClick={() => saveKey(provider.name)}>
                    保存
                  </button>
                  <button onClick={() => testKey(provider.name)}>测试连接</button>
                  {provider.has_key && (
                    <button className="danger" onClick={() => clearKey(provider.name)}>
                      清除
                    </button>
                  )}
                </div>
                {testResults[provider.name] && (
                  <p className="muted" style={{ marginBottom: 0 }}>{testResults[provider.name]}</p>
                )}
              </>
            )}
          </div>
        ))}
      </div>

      <div className="card">
        <h2>成本与预算</h2>
        <div className="row wrap" style={{ alignItems: 'flex-end' }}>
          <label style={{ flex: '0 0 180px' }}>
            <div className="muted">月度预算上限（元，0 = 不限）</div>
            <input value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          <label style={{ flex: '0 0 180px' }}>
            <div className="muted">单次生成成本（元）</div>
            <input value={cost} onChange={(e) => setCost(e.target.value)} />
          </label>
          <label style={{ flex: '0 0 180px' }}>
            <div className="muted">默认面数预算</div>
            <input value={faceBudget} onChange={(e) => setFaceBudget(e.target.value)} />
          </label>
          <label style={{ flex: '1 1 260px' }}>
            <div className="muted">Blender 可执行文件路径（留空自动探测）</div>
            <input value={blender} onChange={(e) => setBlender(e.target.value)} placeholder="C:\\Program Files\\Blender Foundation\\..." />
          </label>
          <button className="primary" onClick={saveNumbers}>
            保存
          </button>
        </div>

        {settings && (
          <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
            本月已用：{settings.usage.this_month.generations} 次生成 · ¥
            {settings.usage.this_month.cost.toFixed(2)}（上限 ¥{settings.monthly_budget_cny.toFixed(2)}）
            · 累计 {settings.usage.all_time.generations} 次
          </p>
        )}
      </div>

      <div className="card">
        <h2>依赖诊断</h2>
        <dl className="kv">
          <dt>版本</dt>
          <dd className="mono">{diagnostics?.version ?? '—'}</dd>
          <dt>Python</dt>
          <dd className="mono">{diagnostics?.python ?? '—'}</dd>
          <dt>减面后端</dt>
          <dd className="mono">
            {diagnostics?.mesh_backends.decimate ?? '—'}
            {diagnostics?.mesh_backends.decimate === '未安装' && (
              <span className="muted"> · pip install -e &quot;.[mesh]&quot;</span>
            )}
          </dd>
          <dt>UV 后端</dt>
          <dd className="mono">
            {diagnostics?.mesh_backends.uv ?? '—'}
            {diagnostics?.mesh_backends.uv === '未安装' && (
              <span className="muted"> · pip install -e &quot;.[mesh]&quot;</span>
            )}
          </dd>
          <dt>Blender</dt>
          <dd className="mono">
            {diagnostics?.blender.available ? diagnostics.blender.path : '未检测到（烘焙与 FBX 导出会降级跳过）'}
          </dd>
        </dl>
        <p className="muted" style={{ marginBottom: 0 }}>
          缺 Blender 不影响主流程：烘焙跳过、转台改用内置软渲染、导出只出 GLB。
          缺 mesh 后端则减面与 UV 会跳过，其余步骤照跑。
        </p>
      </div>
    </div>
  );
}

export default Settings;

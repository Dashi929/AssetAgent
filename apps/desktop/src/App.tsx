/**
 * 应用外壳：侧边导航 + 全局错误提示 + 启动时的初始化。
 *
 * init() 会先解析 sidecar 地址再渲染路由 —— fileUrlSync 依赖这个顺序，
 * 否则端口非默认值时缩略图会 404。
 */

import { useEffect, useState } from 'react';
import { HashRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { useAppStore } from './store/useAppStore';
import Workbench from './pages/Workbench';
import Library from './pages/Library';
import AssetDetail from './pages/AssetDetail';
import Settings from './pages/Settings';

function Sidebar() {
  const sidecar = useAppStore((state) => state.sidecar);
  return (
    <nav className="sidebar">
      <div className="brand">
        <div className="brand-title">AssetAgent</div>
        <div className="brand-sub">概念图 → 引擎资产</div>
      </div>

      <NavLink to="/workbench" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        工作台
      </NavLink>
      <NavLink to="/library" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        资产库
      </NavLink>
      <NavLink to="/settings" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        设置
      </NavLink>

      <div className="spacer" />

      {sidecar && (
        <div style={{ padding: '8px 10px' }}>
          <span className={`badge ${sidecar.state === 'ready' ? 'pass' : sidecar.state === 'starting' ? 'running' : 'fail'}`}>
            本地服务 {sidecar.state === 'ready' ? '正常' : sidecar.state === 'starting' ? '启动中' : '异常'}
          </span>
        </div>
      )}
      <div className="muted" style={{ padding: '0 10px', fontSize: 11 }}>
        v0.1.0 · MVP
      </div>
    </nav>
  );
}

export function App() {
  const { init, error, setError } = useAppStore();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    void (async () => {
      await init();
      setReady(true);
    })();
  }, [init]);

  if (!ready) {
    return (
      <div className="empty" style={{ height: '100vh', display: 'grid', placeItems: 'center' }}>
        正在连接本地服务…
      </div>
    );
  }

  return (
    <HashRouter>
      <div className="app-shell">
        <Sidebar />
        <main className="main">
          {error && (
            <div className="banner error" style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
              <span style={{ flex: 1, whiteSpace: 'pre-wrap' }}>{error}</span>
              <button onClick={() => setError(null)}>关闭</button>
            </div>
          )}

          <Routes>
            <Route path="/" element={<Navigate to="/workbench" replace />} />
            <Route path="/workbench" element={<Workbench />} />
            <Route path="/library" element={<Library />} />
            <Route path="/asset/:assetId" element={<AssetDetail />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/workbench" replace />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
  );
}

export default App;

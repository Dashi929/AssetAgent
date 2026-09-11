import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/global.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('找不到 #root 挂载点，index.html 可能被改坏了');
}

// ---- 闪退防护 + 前端日志 ----
// Electron 默认把拖进窗口的文件当**页面导航**处理：用户把 FBX 拖到拖放区外
// 一点，整个窗口就会被 file:// 替换，看起来就是"闪退"。必须在 window 层拦掉。
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());

// 前端没被 React 接住的错误也落一份到 renderer.log，闪退后有迹可循
function reportError(kind: string, detail: unknown): void {
  const text =
    detail instanceof Error ? `${detail.message}\n${detail.stack ?? ''}` : String(detail);
  window.assetagent?.appendLog(`[${kind}] ${text}`);
}
window.addEventListener('error', (event) => {
  reportError('js-error', `${event.message} @ ${event.filename}:${event.lineno}:${event.colno}`);
});
window.addEventListener('unhandledrejection', (event) => {
  reportError('unhandled-rejection', event.reason);
});

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

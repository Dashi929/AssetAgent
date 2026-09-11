/**
 * preload 注入的桥的全局类型声明。
 *
 * 刻意在这里重写一遍而不是 import electron/preload 的类型 ——
 * 渲染进程和主进程是两套 tsconfig，跨过去 import 会把 Node 类型拖进前端。
 */

export interface SidecarStatus {
  state: 'starting' | 'ready' | 'failed' | 'stopped';
  baseUrl: string;
  pythonPath?: string;
  message?: string;
  logTail?: string;
}

export interface AssetAgentBridge {
  getSidecarStatus: () => Promise<SidecarStatus>;
  restartSidecar: () => Promise<SidecarStatus>;
  getSidecarLog: () => Promise<string>;
  onSidecarStatus: (handler: (status: SidecarStatus) => void) => () => void;
  reveal: (target: string) => Promise<void>;
  platform: string;
}

declare global {
  interface Window {
    /** preload 注入的桥。在浏览器里跑渲染进程时为 undefined。 */
    assetagent?: AssetAgentBridge;
  }
}

export {};

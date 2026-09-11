/**
 * preload：渲染进程唯一能碰到系统能力的窗口。
 *
 * 只暴露必需的四件事。渲染进程拿不到 node 能力，也拿不到完整 IPC ——
 * 这样即使前端将来引入第三方组件，也不会意外获得文件系统权限。
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface SidecarStatus {
  state: 'starting' | 'ready' | 'failed' | 'stopped';
  baseUrl: string;
  pythonPath?: string;
  message?: string;
  logTail?: string;
}

const api = {
  getSidecarStatus: (): Promise<SidecarStatus> => ipcRenderer.invoke('sidecar:status'),
  restartSidecar: (): Promise<SidecarStatus> => ipcRenderer.invoke('sidecar:restart'),
  getSidecarLog: (): Promise<string> => ipcRenderer.invoke('sidecar:log'),
  onSidecarStatus: (handler: (status: SidecarStatus) => void): (() => void) => {
    const listener = (_event: unknown, status: SidecarStatus) => handler(status);
    ipcRenderer.on('sidecar:status', listener);
    return () => ipcRenderer.removeListener('sidecar:status', listener);
  },
  reveal: (target: string): Promise<void> => ipcRenderer.invoke('shell:reveal', target),
  /** 渲染进程写一行日志到 logs/renderer.log（闪退排障用） */
  appendLog: (line: string): void => ipcRenderer.send('logs:append', line),
  /** 设置页"打开日志目录" */
  revealLogs: (): Promise<void> => ipcRenderer.invoke('logs:reveal'),
  platform: process.platform,
};

contextBridge.exposeInMainWorld('assetagent', api);

export type AssetAgentBridge = typeof api;

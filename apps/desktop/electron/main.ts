/**
 * Electron 主进程：窗口 + sidecar 生命周期 + 少量需要系统能力的 IPC。
 *
 * 业务逻辑一律不放这里 —— 主进程只负责"把 sidecar 拉起来、把界面显示出来、
 * 把文件对话框的结果交回去"。渲染进程通过 HTTP 直接和 sidecar 说话。
 */

import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron';
import path from 'node:path';
import { SidecarManager, type SidecarStatus } from './sidecar';

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged;
const repoRoot = isDev ? path.resolve(__dirname, '..', '..', '..') : process.resourcesPath;

const sidecar = new SidecarManager(repoRoot, isDev);
let mainWindow: BrowserWindow | null = null;

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: '#f7f6f3',
    title: 'AssetAgent',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  window.once('ready-to-show', () => window.show());

  // 外部链接走系统浏览器，不在应用内打开
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: 'deny' };
  });

  if (isDev) {
    void window.loadURL('http://127.0.0.1:5173');
  } else {
    void window.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
  }

  return window;
}

async function bootstrap(): Promise<void> {
  mainWindow = createWindow();

  const status = await sidecar.start();
  mainWindow.webContents.once('did-finish-load', () => {
    mainWindow?.webContents.send('sidecar:status', status);
  });

  if (status.state === 'failed') {
    void dialog.showMessageBox({
      type: 'warning',
      title: 'AssetAgent 本地服务启动失败',
      message: status.message ?? '未知原因',
      detail: `${status.logTail ?? ''}\n\n后处理管线与生成功能将不可用。请检查 Python 环境后重启应用。`,
      buttons: ['知道了'],
    });
  }
}

app.whenReady().then(bootstrap).catch((error: unknown) => {
  void dialog.showErrorBox('AssetAgent 启动失败', String(error));
});

app.on('window-all-closed', () => {
  sidecar.stop();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => sidecar.stop());

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow();
});

// ---------------------------------------------------------------- IPC

ipcMain.handle('sidecar:status', (): SidecarStatus => sidecar.getStatus());

ipcMain.handle('sidecar:log', (): string => sidecar.getLogTail());

ipcMain.handle('sidecar:restart', async (): Promise<SidecarStatus> => {
  sidecar.stop();
  return sidecar.start();
});

ipcMain.handle('shell:reveal', async (_event, target: string): Promise<void> => {
  if (!target) return;
  // 文件用"在文件夹中显示"，目录直接打开 —— 用户点"打开导出目录"时期望的是这个
  if (path.extname(target)) {
    shell.showItemInFolder(target);
  } else {
    await shell.openPath(target);
  }
});

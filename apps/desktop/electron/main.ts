/**
 * Electron 主进程：窗口 + sidecar 生命周期 + 少量需要系统能力的 IPC。
 *
 * 业务逻辑一律不放这里 —— 主进程只负责"把 sidecar 拉起来、把界面显示出来、
 * 把文件对话框的结果交回去"。渲染进程通过 HTTP 直接和 sidecar 说话。
 *
 * 日志：所有能想到的"闪退"源头都挂了钩子（未捕获异常 / 渲染进程崩溃 /
 * GPU 等子进程崩溃 / 渲染进程 console / 无响应），见 electron/logging.ts。
 */

import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron';
import path from 'node:path';
import { log, logsDir } from './logging';
import { SidecarManager, type SidecarStatus } from './sidecar';

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged;
const repoRoot = isDev ? path.resolve(__dirname, '..', '..', '..') : process.resourcesPath;

const sidecar = new SidecarManager(repoRoot, isDev, path.join(logsDir(), 'sidecar.log'));
let mainWindow: BrowserWindow | null = null;

// sidecar 状态每次变化都广播给所有窗口（渲染进程可能随时重载，不能只推一次）
sidecar.onStatusChange = (status) => {
  log('main', `sidecar 状态：state=${status.state} baseUrl=${status.baseUrl} message=${status.message ?? '-'}`);
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send('sidecar:status', status);
  }
};

function pageUrl(): string {
  return isDev
    ? 'http://127.0.0.1:5173'
    : 'file://' + path.join(__dirname, '..', 'dist', 'index.html');
}

// ---- 闪退定位：主进程自身的兜底钩子 ----
process.on('uncaughtException', (error) => {
  log('main', `uncaughtException: ${error.stack ?? String(error)}`);
});
process.on('unhandledRejection', (reason) => {
  log('main', `unhandledRejection: ${reason instanceof Error ? reason.stack : String(reason)}`);
});
app.on('child-process-gone', (_event, details) => {
  log(
    'main',
    `child-process-gone: type=${details.type} reason=${details.reason} ` +
      `exitCode=${details.exitCode} ${details.name ?? ''}`,
  );
});

log(
  'main',
  `启动：v${app.getVersion()} packaged=${app.isPackaged} ` +
    `resources=${process.resourcesPath ?? '-'} logs=${logsDir()}`,
);

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

  // ---- 闪退定位：渲染进程侧的钩子 ----
  const wc = window.webContents;
  wc.on('render-process-gone', (_event, details) => {
    log('main', `渲染进程退出：reason=${details.reason} exitCode=${details.exitCode}`);
    // 崩了不闪退：crash/oom 时自动重载窗口救回来；现场已写进日志
    if (details.reason === 'crashed' || details.reason === 'oom') {
      setTimeout(() => {
        if (!window.isDestroyed()) {
          log('main', '渲染进程崩溃，自动重载窗口');
          void window.webContents.loadURL(pageUrl());
        }
      }, 500);
    }
  });
  wc.on('unresponsive', () => log('main', '渲染进程无响应'));
  wc.on('responsive', () => log('main', '渲染进程恢复响应'));
  wc.on('did-fail-load', (_event, code, desc, url) => {
    log('main', `did-fail-load: code=${code} desc=${desc} url=${url}`);
  });
  wc.on('console-message', (_event, level, message, line, sourceId) => {
    // level: 1=info 2=warning 3=error；info 太吵不记
    if (level >= 2) {
      log('main', `renderer console[level=${level}]: ${message} (${sourceId}:${line})`);
    }
  });
  window.on('closed', () => {
    log('main', '主窗口关闭');
    mainWindow = null;
  });

  void window.loadURL(pageUrl());

  return window;
}

async function bootstrap(): Promise<void> {
  mainWindow = createWindow();

  // 状态变化的广播由 onStatusChange 统一发；这里只负责拉起
  const status = await sidecar.start();
  log('main', `sidecar 启动结果：state=${status.state} baseUrl=${status.baseUrl} ` +
    `message=${status.message ?? '-'}\n  logTail=${status.logTail ?? '-'}`);

  // 持久监听（不能用 once）：页面加载常常比 sidecar 启动快，
  // 加载完成时把当前状态补发给前端，避免前端永远停在"连不上"
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow?.webContents.send('sidecar:status', sidecar.getStatus());
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
  log('main', `bootstrap 失败：${error instanceof Error ? error.stack : String(error)}`);
  void dialog.showErrorBox('AssetAgent 启动失败', String(error));
});

app.on('window-all-closed', () => {
  log('main', 'window-all-closed，应用退出');
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

// ---------------------------------------------------------------- 日志 IPC

/** 渲染进程的前端日志（window.assetagent.appendLog）→ renderer.log */
ipcMain.on('logs:append', (_event, line: string) => log('renderer', String(line)));

/** 设置页"打开日志目录" */
ipcMain.handle('logs:reveal', async (): Promise<void> => {
  await shell.openPath(logsDir());
});

/**
 * Electron 主进程：窗口 + sidecar 生命周期 + 少量需要系统能力的 IPC。
 *
 * 业务逻辑一律不放这里 —— 主进程只负责"把 sidecar 拉起来、把界面显示出来、
 * 把文件对话框的结果交回去"。渲染进程通过 HTTP 直接和 sidecar 说话。
 *
 * 日志：所有能想到的"闪退"源头都挂了钩子（未捕获异常 / 渲染进程崩溃 /
 * GPU 等子进程崩溃 / 渲染进程 console / 无响应），见 electron/logging.ts。
 */

import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from 'electron';
import path from 'node:path';
import { log, logsDir } from './logging';
import { SidecarManager, type SidecarStatus } from './sidecar';

// Chromium 内部 UI（页面右键菜单、输入框菜单等）固定为中文 ——
// Electron 自带的英文菜单/右键项跟系统语言走，但应用菜单栏必须自建才有中文
app.commandLine.appendSwitch('lang', 'zh-CN');

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
  // Electron 32 起（.d.ts 未跟上）运行时改传单对象 details：
  // { level: 'info'|'warning'|'error', message, lineNumber, sourceId, frame }；
  // 旧签名是 (event, level: 1..3, message, line, sourceId)。两种形态都归一处理。
  wc.on('console-message', (...args: unknown[]) => {
    const second = args[1] as
      | { level?: string | number; message?: string; lineNumber?: number; sourceId?: string }
      | number
      | undefined;
    const isDetails = typeof second === 'object' && second !== null;
    const lv = isDetails ? second.level : (second as number | undefined);
    // verbose/info 太吵不记
    if (lv !== 'error' && lv !== 'warning' && lv !== 2 && lv !== 3) return;
    const text = String((isDetails ? second.message : args[2]) ?? '');
    const line = Number((isDetails ? second.lineNumber : args[3]) ?? 0);
    const sourceId = String((isDetails ? second.sourceId : args[4]) ?? '');
    log('main', `renderer console[level=${String(lv)}]: ${text} (${sourceId}:${line})`);
  });
  window.on('closed', () => {
    log('main', '主窗口关闭');
    mainWindow = null;
  });

  void window.loadURL(pageUrl());

  return window;
}

// 中文应用菜单：Electron 默认菜单永远是英文，role 只管快捷键/行为，label 全部自己写
function buildApplicationMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: '文件',
      submenu: [{ role: 'quit', label: '退出 AssetAgent' }],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载' },
        { role: 'forceReload', label: '强制重新加载' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '窗口',
      submenu: [
        { role: 'minimize', label: '最小化' },
        { role: 'close', label: '关闭窗口' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function bootstrap(): Promise<void> {
  buildApplicationMenu();
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

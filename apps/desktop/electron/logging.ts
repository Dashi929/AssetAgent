/**
 * 统一日志：主进程 / 渲染进程 / sidecar 三路日志都落在同一个目录。
 *
 * 为什么必须有：桌面应用"闪退"时没有任何现场可查，用户只能干瞪眼。
 * 三个进程各写一个文件（main.log / renderer.log / sidecar.log），
 * 时间戳对齐，闪退后看每个文件的最后几行就能定位是哪一层先出的问题。
 *
 * 目录约定（与 sidecar 数据目录一致，见 services/agent/app/paths.py）：
 * - 打包后：%LOCALAPPDATA%/AssetAgent/logs —— 卸载不带走，用户找得到
 * - 开发时：<repo>/services/agent/.data/logs
 *
 * 写入用 appendFileSync：量小（低频事件），换来"崩的最后一行也一定落盘"。
 */

import { app } from 'electron';
import fs from 'node:fs';
import path from 'node:path';

let cachedDir: string | null = null;

export function logsDir(): string {
  if (cachedDir) return cachedDir;

  let base: string;
  if (app.isPackaged) {
    base = path.join(
      process.env.LOCALAPPDATA ?? app.getPath('appData'),
      'AssetAgent',
    );
  } else {
    // app.getAppPath() = apps/desktop，向上两级是仓库根
    base = path.resolve(app.getAppPath(), '..', '..', 'services', 'agent', '.data');
  }

  const dir = path.join(base, 'logs');
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    /* 目录建不出来时 append 还会再失败一次并被吞掉，这里不放大问题 */
  }
  cachedDir = dir;
  return dir;
}

function timestamp(): string {
  const d = new Date();
  const pad = (n: number, w = 2) => String(n).padStart(w, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.` +
    `${pad(d.getMilliseconds(), 3)}`
  );
}

export function log(channel: 'main' | 'renderer', message: string): void {
  try {
    const clipped = message.length > 2000 ? message.slice(0, 2000) + '…(截断)' : message;
    const file = path.join(logsDir(), channel === 'main' ? 'main.log' : 'renderer.log');
    fs.appendFileSync(file, `[${timestamp()}] ${clipped}\n`, 'utf8');
  } catch {
    /* 日志写不进去绝不能影响主流程 */
  }
}

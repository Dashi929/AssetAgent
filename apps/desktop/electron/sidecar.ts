/**
 * sidecar（Python FastAPI 子进程）的生命周期管理。
 *
 * 设计要点：
 * - sidecar 随应用启动、随应用退出，用户不需要知道它的存在。
 * - 启动失败**不静默**：把可读的原因带到界面上，否则用户只会看到"点了没反应"。
 * - 端口冲突时向上探测，避免和开发时的另一个实例撞车。
 */

import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export interface SidecarStatus {
  state: 'starting' | 'ready' | 'failed' | 'stopped';
  baseUrl: string;
  pythonPath?: string;
  message?: string;
  logTail?: string;
}

const DEFAULT_PORT = 8756;
const PORT_SCAN_RANGE = 12;
const READY_TIMEOUT_MS = 60_000;

export class SidecarManager {
  private child: ChildProcess | null = null;
  private logBuffer: string[] = [];
  private logFd: number | null = null;
  private status: SidecarStatus = { state: 'stopped', baseUrl: '' };

  /** 状态每次变化都会回调（main 用来广播给所有窗口） */
  onStatusChange: ((status: SidecarStatus) => void) | null = null;

  constructor(
    private readonly repoRoot: string,
    private readonly isDev: boolean,
    private readonly logFile?: string,
  ) {}

  private setStatus(next: SidecarStatus): void {
    this.status = next;
    try {
      this.onStatusChange?.(next);
    } catch {
      /* 回调异常不影响 sidecar 管理 */
    }
  }

  getStatus(): SidecarStatus {
    return this.status;
  }

  async start(): Promise<SidecarStatus> {
    this.setStatus({ state: 'starting', baseUrl: '' });

    const exec = this.resolveExecutable();
    if (!exec) {
      return this.fail(
        '找不到 sidecar 可执行文件。请确认安装包完整，' +
          '或设置环境变量 ASSETAGENT_PYTHON 指向 python.exe。',
      );
    }

    const port = await this.findFreePort();
    const baseUrl = `http://127.0.0.1:${port}`;

    try {
      const args = exec.type === 'exe'
        ? []  // .exe 内置了 uvicorn 启动逻辑
        : ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)];

      // sidecar 自己的输出以前只进内存缓冲，应用一闪退就全没了；
      // 现在同步落盘到 logs/sidecar.log，和主进程日志时间戳对齐
      if (this.logFile) {
        try {
          this.logFd = fs.openSync(this.logFile, 'a');
          fs.writeSync(
            this.logFd,
            `\n---- sidecar 启动 ${new Date().toISOString()} type=${exec.type} port=${port} ----\n`,
          );
        } catch {
          this.logFd = null; // 日志写不进去不影响功能
        }
      }

      this.child = spawn(exec.path, args, {
        cwd: exec.type === 'python' ? this.sidecarDir() : path.dirname(exec.path),
        env: {
          ...process.env,
          ASSETAGENT_PORT: String(port),
          ASSETAGENT_HOST: '127.0.0.1',
          PYTHONUNBUFFERED: '1',
          PYTHONIOENCODING: 'utf-8',
        },
        windowsHide: true,
      });
    } catch (error) {
      return this.fail(`无法启动 sidecar 进程：${String(error)}`);
    }

    this.child.stdout?.on('data', (chunk: Buffer) => this.appendLog(chunk.toString()));
    this.child.stderr?.on('data', (chunk: Buffer) => this.appendLog(chunk.toString()));
    this.child.on('exit', (code, signal) => {
      this.writeLog(`---- sidecar 退出 code=${code} signal=${signal ?? '-'} ----\n`);
      this.closeLog();
      if (this.status.state !== 'stopped') {
        this.setStatus({
          ...this.status,
          state: 'failed',
          message: `sidecar 进程意外退出（退出码 ${code}）`,
          logTail: this.tail(),
        });
      }
      this.child = null;
    });

    const ready = await this.waitUntilReady(baseUrl);
    if (!ready) {
      return this.fail('sidecar 启动超时（60 秒内没有响应健康检查）');
    }

    this.setStatus({ state: 'ready', baseUrl, pythonPath: exec.path });
    return this.status;
  }

  stop(): void {
    this.setStatus({ ...this.status, state: 'stopped' });
    if (!this.child) {
      this.closeLog();
      return;
    }
    try {
      // Windows 下 SIGTERM 不一定能穿透到 Python，先试优雅退出再强杀
      this.child.kill();
      setTimeout(() => this.child?.kill('SIGKILL'), 3000);
    } catch {
      /* 退出阶段的异常没有处理价值 */
    }
    this.child = null;
  }

  /** 让用户能在设置页看到 sidecar 的真实日志，而不是干瞪眼。 */
  getLogTail(): string {
    return this.tail();
  }

  private fail(message: string): SidecarStatus {
    this.setStatus({ ...this.status, state: 'failed', message, logTail: this.tail() });
    return this.status;
  }

  private appendLog(text: string): void {
    this.writeLog(text);
    for (const line of text.split(/\r?\n/)) {
      if (line.trim()) this.logBuffer.push(line.trim());
    }
    if (this.logBuffer.length > 400) this.logBuffer = this.logBuffer.slice(-400);
  }

  private writeLog(text: string): void {
    if (this.logFd === null) return;
    try {
      fs.writeSync(this.logFd, text);
    } catch {
      /* 单次写失败不重试，日志不能反噬主流程 */
    }
  }

  private closeLog(): void {
    if (this.logFd === null) return;
    try {
      fs.closeSync(this.logFd);
    } catch {
      /* 已关闭 */
    }
    this.logFd = null;
  }

  private tail(lines = 20): string {
    return this.logBuffer.slice(-lines).join('\n');
  }

  private sidecarDir(): string {
    // 打包后 sidecar 源码在 resources/sidecar；开发时在仓库的 services/agent
    const packaged = path.join(process.resourcesPath ?? '', 'sidecar');
    if (!this.isDev && fs.existsSync(path.join(packaged, 'app', 'main.py'))) {
      return packaged;
    }
    return path.join(this.repoRoot, 'services', 'agent');
  }

  private resolveExecutable(): { type: 'exe' | 'python'; path: string } | null {
    // 1) 打包后的独立 .exe（优先级最高）
    if (!this.isDev) {
      const packagedExe = path.join(process.resourcesPath ?? '', 'sidecar-dist', 'assetagent-sidecar.exe');
      if (fs.existsSync(packagedExe)) {
        return { type: 'exe', path: packagedExe };
      }
    }

    // 2) 开发时直接构建的 .exe
    const devExe = path.join(this.repoRoot, 'apps', 'desktop', 'sidecar-dist', 'assetagent-sidecar.exe');
    if (fs.existsSync(devExe)) {
      return { type: 'exe', path: devExe };
    }

    // 3) 环境变量指定
    if (process.env.ASSETAGENT_PYTHON) {
      if (fs.existsSync(process.env.ASSETAGENT_PYTHON)) {
        return { type: 'python', path: process.env.ASSETAGENT_PYTHON };
      }
    }

    // 4) venv
    const venvNames = ['Scripts/python.exe', 'bin/python3', 'bin/python'];
    for (const rel of venvNames) {
      const inSidecar = path.join(this.sidecarDir(), '.venv', rel);
      if (fs.existsSync(inSidecar)) return { type: 'python', path: inSidecar };

      const inRepo = path.join(this.repoRoot, 'services', 'agent', '.venv', rel);
      if (fs.existsSync(inRepo)) return { type: 'python', path: inRepo };
    }

    // 5) 系统 PATH
    if (process.platform === 'win32') {
      return { type: 'python', path: 'python.exe' };
    }
    return { type: 'python', path: 'python3' };
  }

  private async findFreePort(): Promise<number> {
    for (let offset = 0; offset < PORT_SCAN_RANGE; offset += 1) {
      const port = DEFAULT_PORT + offset;
      if (await this.isPortFree(port)) return port;
    }
    return DEFAULT_PORT + PORT_SCAN_RANGE;
  }

  private async isPortFree(port: number): Promise<boolean> {
    const net = await import('node:net');
    return new Promise((resolve) => {
      const server = net.createServer();
      server.once('error', () => resolve(false));
      server.once('listening', () => server.close(() => resolve(true)));
      server.listen(port, '127.0.0.1');
    });
  }

  private async waitUntilReady(baseUrl: string): Promise<boolean> {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (this.status.state === 'failed') return false;
      try {
        const response = await fetch(`${baseUrl}/api/health`);
        if (response.ok) return true;
      } catch {
        /* 还没起来，继续等 */
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return false;
  }
}

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
  private status: SidecarStatus = { state: 'stopped', baseUrl: '' };

  constructor(private readonly repoRoot: string, private readonly isDev: boolean) {}

  getStatus(): SidecarStatus {
    return this.status;
  }

  async start(): Promise<SidecarStatus> {
    this.status = { state: 'starting', baseUrl: '' };

    const python = this.resolvePython();
    if (!python) {
      return this.fail(
        '找不到 Python 解释器。请设置环境变量 ASSETAGENT_PYTHON 指向 python.exe，' +
          '或在 services/agent 下创建 .venv 虚拟环境。',
      );
    }

    const port = await this.findFreePort();
    const baseUrl = `http://127.0.0.1:${port}`;

    try {
      this.child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
        cwd: this.sidecarDir(),
        env: {
          ...process.env,
          ASSETAGENT_PORT: String(port),
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
    this.child.on('exit', (code) => {
      if (this.status.state !== 'stopped') {
        this.status = {
          ...this.status,
          state: 'failed',
          message: `sidecar 进程意外退出（退出码 ${code}）`,
          logTail: this.tail(),
        };
      }
      this.child = null;
    });

    const ready = await this.waitUntilReady(baseUrl);
    if (!ready) {
      return this.fail('sidecar 启动超时（60 秒内没有响应健康检查）');
    }

    this.status = { state: 'ready', baseUrl, pythonPath: python };
    return this.status;
  }

  stop(): void {
    this.status = { ...this.status, state: 'stopped' };
    if (!this.child) return;
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
    this.status = { ...this.status, state: 'failed', message, logTail: this.tail() };
    return this.status;
  }

  private appendLog(text: string): void {
    for (const line of text.split(/\r?\n/)) {
      if (line.trim()) this.logBuffer.push(line.trim());
    }
    if (this.logBuffer.length > 400) this.logBuffer = this.logBuffer.slice(-400);
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

  private resolvePython(): string | null {
    const candidates: string[] = [];

    if (process.env.ASSETAGENT_PYTHON) {
      candidates.push(process.env.ASSETAGENT_PYTHON);
    }

    const venvNames = ['Scripts/python.exe', 'bin/python3', 'bin/python'];
    for (const rel of venvNames) {
      candidates.push(path.join(this.sidecarDir(), '.venv', rel));
      candidates.push(path.join(this.repoRoot, 'services', 'agent', '.venv', rel));
    }

    if (process.platform === 'win32') {
      candidates.push('python.exe', 'python3.exe');
    } else {
      candidates.push('python3', 'python');
    }

    for (const candidate of candidates) {
      if (candidate.includes(path.sep) || candidate.includes('/')) {
        if (fs.existsSync(candidate)) return candidate;
      } else {
        return candidate; // 交给 PATH 解析
      }
    }
    return null;
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

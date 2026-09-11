/**
 * 全局状态。
 *
 * 只放"跨页面共享"的东西：sidecar 状态、资产列表、当前资产详情、预设、设置、当前任务。
 * 页面内部的临时状态（表单、选中项）一律留在组件里 —— 全局状态越薄越好维护。
 */

import { create } from 'zustand';
import { api, ApiError } from '../api/client';
import type {
  AssetDetail,
  AssetSummary,
  Job,
  PresetsResponse,
  SettingsSnapshot,
} from '../api/types';
import type { SidecarStatus } from '../global';

interface AppState {
  sidecar: SidecarStatus | null;
  assets: AssetSummary[];
  detail: AssetDetail | null;
  presets: PresetsResponse | null;
  settings: SettingsSnapshot | null;
  job: Job | null;
  error: string | null;
  busy: boolean;

  init: () => Promise<void>;
  refreshAssets: () => Promise<void>;
  openAsset: (assetId: string) => Promise<void>;
  closeAsset: () => void;
  refreshSettings: () => Promise<void>;
  refreshPresets: () => Promise<void>;
  trackJob: (job: Job) => void;
  clearJob: () => void;
  setError: (message: string | null) => void;
  handle: <T>(action: () => Promise<T>) => Promise<T | null>;
}

let pollTimer: ReturnType<typeof setInterval> | null = null;

export const useAppStore = create<AppState>((set, get) => ({
  sidecar: null,
  assets: [],
  detail: null,
  presets: null,
  settings: null,
  job: null,
  error: null,
  busy: false,

  async init() {
    const bridge = window.assetagent;
    if (bridge) {
      const status = await bridge.getSidecarStatus();
      set({ sidecar: status });
      bridge.onSidecarStatus((next) => set({ sidecar: next }));
    }
    await get().handle(async () => {
      await Promise.all([get().refreshAssets(), get().refreshPresets(), get().refreshSettings()]);
    });
  },

  async refreshAssets() {
    set({ assets: await api.listAssets() });
  },

  async openAsset(assetId) {
    await get().handle(async () => {
      set({ detail: await api.getAsset(assetId) });
    });
  },

  closeAsset() {
    set({ detail: null });
  },

  async refreshSettings() {
    set({ settings: await api.getSettings() });
  },

  async refreshPresets() {
    set({ presets: await api.getPresets() });
  },

  /**
   * 跟踪一个后台任务。
   *
   * 任务状态在 sidecar 侧是落盘的，所以哪怕前端刷新，这里重新挂上也能接着看进度 ——
   * 前端不需要自己维护"任务进行到哪了"。
   */
  trackJob(job) {
    if (pollTimer) clearInterval(pollTimer);
    set({ job });

    pollTimer = setInterval(async () => {
      try {
        const latest = await api.getJob(job.id);
        set({ job: latest });

        if (latest.status === 'succeeded' || latest.status === 'failed' || latest.status === 'cancelled') {
          if (pollTimer) clearInterval(pollTimer);
          pollTimer = null;
          if (latest.status === 'failed' && latest.error) {
            set({ error: latest.error });
          }
          // 任务结束：刷新资产与详情，让界面反映最新状态
          await get().refreshAssets();
          const detail = get().detail;
          if (detail && detail.asset.id === latest.asset_id) {
            await get().openAsset(latest.asset_id);
          }
        }
      } catch (error) {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        set({ error: error instanceof Error ? error.message : String(error) });
      }
    }, 700);
  },

  clearJob() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    set({ job: null });
  },

  setError(message) {
    set({ error: message });
  },

  /** 统一处理异步动作的错误与 loading，避免每个调用点都写一遍 try/catch。 */
  async handle(action) {
    set({ busy: true, error: null });
    try {
      const result = await action();
      return result;
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : String(error);
      set({ error: message });
      return null;
    } finally {
      set({ busy: false });
    }
  },
}));

/**
 * sidecar HTTP 客户端。
 *
 * baseUrl 在运行时解析（sidecar 端口可能因为冲突而上浮），解析一次后缓存。
 * 所有错误统一转成 ApiError，把 sidecar 返回的中文 detail 原样带出来 ——
 * 后端写的错误信息是给人看的，前端不该把它换成一个通用文案。
 */

import type {
  AssetDetail,
  AssetSummary,
  Diagnostics,
  ExportRecord,
  Job,
  PresetsResponse,
  SettingsSnapshot,
  SpecPreset,
  TelemetrySummary,
  ValidationReport,
} from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let cachedBaseUrl: string | null = null;

export async function resolveBaseUrl(): Promise<string> {
  if (cachedBaseUrl) return cachedBaseUrl;

  const bridge = (window as unknown as { assetagent?: { getSidecarStatus: () => Promise<{ state: string; baseUrl: string }> } })
    .assetagent;
  if (bridge) {
    try {
      const status = await bridge.getSidecarStatus();
      if (status.state === 'ready' && status.baseUrl) {
        cachedBaseUrl = status.baseUrl;
        return cachedBaseUrl;
      }
    } catch {
      /* 浏览器里开发时没有 bridge，走默认端口 */
    }
  }
  // ⚠️ sidecar 还没就绪时不缓存 fallback：它的真实端口可能不是 8756
  //（冲突上浮），等状态 ready 后下一次请求会重新解析并缓存正确的地址
  return 'http://127.0.0.1:8756';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = await resolveBaseUrl();
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...init,
      headers: {
        ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...init?.headers,
      },
    });
  } catch (error) {
    // 网络层失败：端口可能已经变了（sidecar 重启换端口），清掉缓存让下次重新解析
    cachedBaseUrl = null;
    throw new ApiError(
      `连不上本地服务（${base}）。请确认 sidecar 已启动；如果反复失败，可在设置页查看日志。`,
      0,
    );
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      /* 响应不是 JSON，用默认文案 */
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** 产物文件通过 sidecar 读取（渲染进程拿不到 file:// 权限）。 */
export async function fileUrl(absolutePath: string): Promise<string> {
  const base = await resolveBaseUrl();
  return `${base}/api/files?path=${encodeURIComponent(absolutePath)}`;
}

/**
 * 同步版本的 fileUrl，给 <img src> 这类拿不到 await 的场景用。
 *
 * 依赖 init() 已经先跑过 resolveBaseUrl()（App 会保证这一点），
 * 否则端口不是默认值时图片会 404。这是刻意的取舍：让 99% 的渲染路径保持简单。
 */
export function fileUrlSync(absolutePath: string): string {
  const base = cachedBaseUrl ?? 'http://127.0.0.1:8756';
  return `${base}/api/files?path=${encodeURIComponent(absolutePath)}`;
}

// ---------------------------------------------------------------- 资产

export const api = {
  listAssets: (includeArchived = false) =>
    request<AssetSummary[]>(`/api/assets?include_archived=${includeArchived}`),

  getAsset: (assetId: string) => request<AssetDetail>(`/api/assets/${assetId}`),

  createAssetFromImages: (files: File[], name: string, presetKey: string, prompt = '') => {
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    form.append('name', name);
    form.append('preset_key', presetKey);
    form.append('prompt', prompt);
    return request<AssetSummary>('/api/assets/upload', { method: 'POST', body: form });
  },

  createAssetFromMesh: (file: File, name: string, presetKey: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('name', name);
    form.append('preset_key', presetKey);
    return request<AssetSummary>('/api/assets/import-mesh', { method: 'POST', body: form });
  },

  generate: (assetId: string, numVariants: number, provider?: string, prompt = '') =>
    request<{ job: Job; provider: string; estimate_cny: number }>(`/api/assets/${assetId}/generate`, {
      method: 'POST',
      body: JSON.stringify({ num_variants: numVariants, provider: provider ?? null, prompt }),
    }),

  pickVariant: (assetId: string, variantId: string) =>
    request<AssetSummary>(`/api/assets/${assetId}/pick`, {
      method: 'POST',
      body: JSON.stringify({ variant_id: variantId }),
    }),

  runPipeline: (assetId: string, steps?: string[]) =>
    request<{ job: Job }>(`/api/assets/${assetId}/pipeline`, {
      method: 'POST',
      body: JSON.stringify({ steps: steps ?? null }),
    }),

  exportAsset: (assetId: string, preset: string, allowFailedExport = false) =>
    request<{ export: ExportRecord; warnings: string[]; manifest: Record<string, unknown> }>(
      `/api/assets/${assetId}/export`,
      { method: 'POST', body: JSON.stringify({ preset, validate_first: true, allow_failed_export: allowFailedExport }) },
    ),

  archiveAsset: (assetId: string) =>
    request<AssetSummary>(`/api/assets/${assetId}/archive`, { method: 'POST' }),

  retryAsset: (assetId: string) =>
    request<AssetSummary>(`/api/assets/${assetId}/retry`, { method: 'POST' }),

  // ---------------------------------------------------------------- 任务

  getJob: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),

  cancelJob: (jobId: string) => request<{ cancelled: boolean }>(`/api/jobs/${jobId}/cancel`, { method: 'POST' }),

  // ---------------------------------------------------------------- 设置与元信息

  getSettings: () => request<SettingsSnapshot>('/api/settings'),

  patchSettings: (patch: Partial<SettingsSnapshot>) =>
    request<SettingsSnapshot>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),

  setByokKey: (provider: string, key: string) =>
    request<SettingsSnapshot>('/api/settings/byok', {
      method: 'PUT',
      body: JSON.stringify({ provider, key }),
    }),

  clearByokKey: (provider: string) =>
    request<SettingsSnapshot>(`/api/settings/byok/${provider}`, { method: 'DELETE' }),

  testProvider: (provider: string) =>
    request<{ provider: string; ok: boolean; message: string }>(`/api/settings/byok/${provider}/test`, {
      method: 'POST',
    }),

  getPresets: () => request<PresetsResponse>('/api/presets'),

  getDiagnostics: () => request<Diagnostics>('/api/diagnostics'),

  getReports: async (assetId: string): Promise<ValidationReport[]> =>
    (await request<AssetDetail>(`/api/assets/${assetId}`)).reports,

  // ---------------------------------------------------------------- 埋点

  getTelemetrySummary: (month?: string) =>
    request<TelemetrySummary>(`/api/telemetry/summary${month ? `?month=${encodeURIComponent(month)}` : ''}`),

  /** 原始埋点 NDJSON 文本（用户手动导出上报用）。 */
  exportTelemetryRaw: async (month?: string): Promise<string> => {
    const base = await resolveBaseUrl();
    const response = await fetch(
      `${base}/api/telemetry/export${month ? `?month=${encodeURIComponent(month)}` : ''}`,
    );
    if (!response.ok) throw new ApiError(`请求失败（HTTP ${response.status}）`, response.status);
    return response.text();
  },
};

export type { SpecPreset };

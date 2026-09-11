/**
 * 与 sidecar 的数据契约。
 *
 * 这些类型是 app/models.py 的手工镜像 —— 对应产品策划文档 5.4.4 冻结的数据模型。
 * 改这里之前先改 Python 那边的模型，两边必须一起动。
 */

export type AssetStatus =
  | 'draft'
  | 'generating'
  | 'awaiting_pick'
  | 'processing'
  | 'awaiting_validation'
  | 'validated'
  | 'exported'
  | 'failed'
  | 'archived';

export type AssetSource = 'image' | 'text' | 'mesh';

export type VersionOp = 'import' | 'generate' | 'repair' | 'decimate' | 'uv' | 'bake' | 'export';

export type JobStep =
  | 'generate'
  | 'repair'
  | 'decimate'
  | 'uv'
  | 'bake'
  | 'validate'
  | 'export'
  | 'render'
  | 'pipeline';

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export type CheckResult = 'pass' | 'fail' | 'warn' | 'skipped';

export type TargetEngine = 'unity' | 'unreal' | 'generic';

export interface SpecPreset {
  name: string;
  category: string;
  face_budget: number;
  want_quads: boolean;
  target_engine: TargetEngine;
  unit_scale: number;
  expected_size_m: number | null;
  texture_resolution: number;
  pivot: 'bottom_center' | 'origin' | 'custom';
  naming_pattern: string;
}

export interface Asset {
  id: string;
  name: string;
  style_id: string | null;
  source: AssetSource;
  status: AssetStatus;
  spec: SpecPreset;
  source_files: string[];
  picked_variant_id: string | null;
  head_version_id: string | null;
  prompt: string;
  notes: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}

export interface Variant {
  id: string;
  asset_id: string;
  provider: string;
  params: Record<string, unknown>;
  mesh_path: string;
  thumbnail_path: string | null;
  turntable_paths: string[];
  cost: number;
  face_count: number | null;
  created_at: string;
}

export interface VersionNode {
  id: string;
  asset_id: string;
  parent_id: string | null;
  op: VersionOp;
  label: string;
  params: Record<string, unknown>;
  mesh_path: string;
  stats: Record<string, unknown>;
  skipped_reason: string | null;
  created_at: string;
}

export interface Job {
  id: string;
  asset_id: string;
  step: JobStep;
  status: JobStatus;
  progress: number;
  message: string;
  cost: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  running?: boolean;
}

export interface Locator {
  kind: 'faces' | 'uv_islands' | 'none';
  indices: number[];
  uv_bbox: [number, number, number, number] | null;
}

export interface RuleResult {
  rule: string;
  label: string;
  result: CheckResult;
  value: unknown;
  threshold: unknown;
  message: string;
  locator: Locator;
}

export interface ValidationReport {
  id: string;
  asset_id: string;
  version_id: string;
  ruleset: string;
  passed: boolean;
  results: RuleResult[];
  stats: Record<string, unknown>;
  created_at: string;
}

export interface ExportRecord {
  id: string;
  asset_id: string;
  version_id: string;
  preset: string;
  files: string[];
  warnings: string[];
  created_at: string;
}

export interface ProviderInfo {
  name: string;
  display_name: string;
  mode: 'byok' | 'relay' | 'mock';
  available: boolean;
  has_key: boolean;
  cost_per_generation: number;
  capabilities: string[];
  note: string;
  key_masked?: string;
}

export interface AssetSummary {
  asset: Asset;
  counts: { variants: number; versions: number; exports: number };
  thumbnail: string | null;
  validation: {
    id: string;
    passed: boolean;
    created_at: string;
    failed_rules: string[];
  } | null;
}

export interface AssetDetail extends AssetSummary {
  variants: Variant[];
  versions: VersionNode[];
  reports: ValidationReport[];
  exports: ExportRecord[];
  jobs: Job[];
}

export interface SettingsSnapshot {
  route_mode: 'byok' | 'relay';
  allow_mock_fallback: boolean;
  monthly_budget_cny: number;
  cost_per_generation_cny: number;
  default_face_budget: number;
  blender_bin: string;
  data_dir: string;
  providers: ProviderInfo[];
  usage: {
    month: string;
    this_month: { month: string; generations: number; cost: number };
    all_time: { month: null; generations: number; cost: number };
  };
}

export interface Diagnostics {
  version: string;
  python: string;
  data_dir: string;
  recipes_dir: string;
  route_mode: string;
  blender: { available: boolean; path: string | null };
  mesh_backends: { decimate: string; uv: string };
  providers: ProviderInfo[];
}

export interface PresetsResponse {
  spec_presets: Record<string, SpecPreset>;
  export_presets: Record<string, { display_name: string; up_axis?: string; unit?: string; notes?: string }>;
}

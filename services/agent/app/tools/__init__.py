"""后处理工具层 —— 管线各步骤的实现。

每个步骤都是"输入网格 → 输出网格 + 报告"的纯函数（除了烘焙与导出要落盘），
编排逻辑在 pipeline.py，这里只管单步怎么做。
"""

from .blender import BlenderUnavailable
from .blender import available as blender_available
from .convert import convert_to_glb
from .decimate import backend_name as decimate_backend
from .decimate import decimate_mesh
from .export import export_asset
from .mesh_io import MeshError, load_mesh, mesh_stats, save_mesh
from .pipeline import DEFAULT_STEPS, PipelineError, resolve_input, run_pipeline
from .render import render_thumbnail, render_turntable
from .repair import normalize_transform, repair_mesh
from .uv import unwrap, uv_island_margin_px, uv_islands, uv_overlap
from .validate import load_rules, summarize, validate

__all__ = [
    "DEFAULT_STEPS",
    "BlenderUnavailable",
    "MeshError",
    "PipelineError",
    "blender_available",
    "convert_to_glb",
    "decimate_backend",
    "decimate_mesh",
    "export_asset",
    "load_mesh",
    "load_rules",
    "mesh_stats",
    "normalize_transform",
    "render_thumbnail",
    "render_turntable",
    "repair_mesh",
    "resolve_input",
    "run_pipeline",
    "save_mesh",
    "summarize",
    "unwrap",
    "uv_island_margin_px",
    "uv_islands",
    "uv_overlap",
    "validate",
]

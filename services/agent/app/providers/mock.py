"""离线占位 Provider。

作用有两个，都很关键：
1. 让整条后处理管线在**没有任何 API Key** 的情况下可测 —— 冒烟测试和工作流 C 靠它。
2. 让前端开发不依赖任何 Provider 的可用性。

它生成的网格是刻意"脏"的（多个连通分量、退化面、无 UV、高面数），
这样修复/减面/UV 三步都有真实工作量，不会被一个干净网格假通过。
"""

from __future__ import annotations

import asyncio

import numpy as np
import trimesh

from ..models import SpecPreset
from .base import Gen3DProvider, GenerateRequest, VariantResult


def _placeholder_mesh(seed: int, spec: SpecPreset) -> trimesh.Trimesh:
    rng = np.random.default_rng(1000 + seed)

    # 高面数基础体（subdivisions=4 → 5120 面），保证减面步骤有真实工作量
    base = trimesh.creation.icosphere(subdivisions=4, radius=0.5)
    verts = base.vertices.copy().astype(np.float64)

    # 各向异性缩放 + 锥化 + 噪声，让每个变体看起来是不同的"道具"而不是同一个球
    scale = rng.uniform(0.7, 1.3, size=3)
    verts *= scale
    taper = 1.0 + 0.45 * rng.uniform(-1.0, 1.0) * (verts[:, 2] / 0.6)
    verts[:, :2] *= taper[:, None]
    verts += rng.normal(0.0, 0.012, verts.shape)

    # 底面压到 z=0，符合"轴心在底面中心"的默认规范
    verts[:, 2] -= verts[:, 2].min()

    # 故意塞一个游离小组件：修复步骤应该把它清掉
    stray = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    stray.apply_translation([0.95, 0.0, 0.03])
    merged = trimesh.util.concatenate(
        [trimesh.Trimesh(vertices=verts, faces=base.faces.copy(), process=False), stray]
    )

    # 故意塞一个退化面（三个顶点共线）：修复步骤应该把它清掉
    degenerate = np.array([[0, 0, 1]], dtype=np.int64)
    faces = np.vstack([merged.faces, degenerate])

    mesh = trimesh.Trimesh(vertices=merged.vertices, faces=faces, process=False)
    mesh.metadata["source"] = f"mock/seed={seed}"
    mesh.metadata["target_face_budget"] = spec.face_budget
    return mesh


def _export(mesh: trimesh.Trimesh, out_dir, stem: str):
    """优先 GLB（预览格式统一 GLB），失败退回 OBJ。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    glb = out_dir / f"{stem}.glb"
    try:
        glb.write_bytes(mesh.export(file_type="glb"))
        return glb
    except Exception:
        obj = out_dir / f"{stem}.obj"
        obj.write_text(mesh.export(file_type="obj"), encoding="utf-8")
        return obj


class MockProvider(Gen3DProvider):
    name = "mock"
    display_name = "Mock（离线占位）"
    capabilities = ("image_to_3d", "text_to_3d", "offline")
    note = "本地生成占位网格，不发起任何网络请求。无 Key 时用它跑通全流程。"

    @property
    def has_key(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return True

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        spec = req.spec or SpecPreset()
        results: list[VariantResult] = []
        for index in range(max(1, req.num_variants)):
            mesh = await asyncio.to_thread(_placeholder_mesh, index, spec)
            path = await asyncio.to_thread(
                _export, mesh, req.out_dir, f"variant_{index + 1:02d}"
            )
            results.append(
                VariantResult(
                    provider=self.name,
                    mesh_path=path,
                    params={
                        "seed": index,
                        "mode": "offline-placeholder",
                        "prompt": req.prompt,
                        "image_count": len(req.image_paths),
                        "spec": self._spec_dict(req),
                    },
                    cost=0.0,
                    raw={"note": "mock provider，未调用任何外部服务"},
                )
            )
        return results

    def estimate_cost(self, num_variants: int, unit_cost: float) -> float:
        return 0.0  # 离线占位不产生任何费用，界面必须如实显示 ¥0

    async def healthcheck(self) -> bool:
        return True

    def info(self):
        info = super().info()
        info.mode = "mock"  # type: ignore[assignment]
        return info


__all__ = ["MockProvider"]

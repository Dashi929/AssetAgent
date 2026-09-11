"""UV 岛重打包测试。

用手工构造的 UV 直接驱动 repack_uv_islands，不依赖 xatlas 的非确定行为：
- 有重叠的岛布局 → 重排后检测器报零；
- 岛间距 ≥ 2×margin（包围盒之间的空隙是构造保证）；
- 岛数量与面归属在重排前后一致（只动 UV，不动拓扑）；
- 无 UV 时如实 no-op。
"""

from __future__ import annotations

import numpy as np
import trimesh

from app.tools.uv import repack_uv_islands, uv_island_margin_px, uv_islands, uv_overlap


def _two_triangle_mesh(uv_a: list[tuple[float, float]], uv_b: list[tuple[float, float]]) -> trimesh.Trimesh:
    """两个互不相连的三角形，UV 分别指定 —— 岛划分完全由 UV 连通性决定。"""
    vertices = np.asarray(
        [
            [0, 0, 0], [1, 0, 0], [0, 1, 0],  # 三角 A
            [5, 0, 0], [6, 0, 0], [5, 1, 0],  # 三角 B（空间上分开，UV 上可以重叠）
        ],
        dtype=np.float64,
    )
    faces = np.asarray([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    uv = np.asarray(uv_a + uv_b, dtype=np.float64)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
    return mesh


def test_repack_resolves_overlapping_islands():
    # 三角 B 从上方压进 A 的上尖角 —— 重排后由构造保证分离
    uv_a = [(0.1, 0.1), (0.5, 0.1), (0.1, 0.5)]
    uv_b = [(0.02, 0.25), (0.62, 0.25), (0.02, 0.85)]
    mesh = _two_triangle_mesh(uv_a, uv_b)

    before = uv_overlap(mesh)
    assert before["computable"] and before["count"] > 0, "测试前提：构造出重叠"
    islands_before = len(uv_islands(mesh))

    mesh, report = repack_uv_islands(mesh)

    assert report["applied"] is True
    assert report["remaining"] == 0, report
    assert uv_overlap(mesh)["count"] == 0
    # 拓扑不变：还是 2 个岛
    assert len(uv_islands(mesh)) == islands_before
    # 全部落回 [0,1]²
    uv = mesh.visual.uv
    assert uv.min() >= 0.0 and uv.max() <= 1.0


def test_repack_guarantees_island_margin():
    uv_a = [(0.0, 0.0), (0.4, 0.0), (0.0, 0.4)]
    uv_b = [(0.3, 0.3), (0.7, 0.3), (0.3, 0.7)]
    mesh = _two_triangle_mesh(uv_a, uv_b)

    mesh, report = repack_uv_islands(mesh, margin_px=6.0, reference_resolution=4096)

    assert report["applied"] is True
    px = uv_island_margin_px(mesh, 4096)
    assert px is not None
    # 相邻岛包围盒之间的空隙就是 margin（游标推进含单边留白），测出来应 ≥ margin
    assert px >= 6.0 - 0.5, f"岛间距 {px}px 不足"


def test_repack_is_noop_on_clean_layout_keeps_scale():
    uv_a = [(0.05, 0.05), (0.25, 0.05), (0.05, 0.25)]
    uv_b = [(0.6, 0.6), (0.9, 0.6), (0.6, 0.9)]
    mesh = _two_triangle_mesh(uv_a, uv_b)

    mesh, report = repack_uv_islands(mesh, margin_px=2.0, reference_resolution=1024)

    assert report["applied"] is True  # 重排是布局层面的操作，干净布局也会规范化
    assert report["scale"] == 1.0  # 放得下就不缩
    assert uv_overlap(mesh)["count"] == 0


def test_repack_without_uv_is_a_noop():
    mesh = trimesh.creation.box()

    mesh, report = repack_uv_islands(mesh)

    assert report["applied"] is False
    assert "没有 UV" in report.get("reason", "")


def test_repack_scales_down_when_space_is_tight():
    """一堆积木占的面积放不进 [0,1]² 时整体等比缩，而不是失败。"""
    mesh = trimesh.Trimesh(
        vertices=np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0], [3, 0, 0], [2, 1, 0]], dtype=np.float64),
        faces=np.asarray([[0, 1, 2], [3, 4, 5]], dtype=np.int64),
        process=False,
    )
    # 两个大三角形，原尺寸总宽 3.0 > 1.0，必须缩
    uv = np.asarray([[0, 0], [1, 0], [0, 1], [0, 0], [1, 0], [0, 1]], dtype=np.float64)
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv)

    mesh, report = repack_uv_islands(mesh, margin_px=4.0, reference_resolution=1024)

    assert report["applied"] is True
    assert report["scale"] < 1.0
    assert uv_overlap(mesh)["count"] == 0
    assert mesh.visual.uv.max() <= 1.0

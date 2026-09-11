"""UV 去重叠后处理测试。

用手工构造的 UV 直接驱动 deoverlap_uv，不依赖 xatlas 的非确定行为：
- 两片 UV 重叠但岛心分离 → 应收缩到检测器报零；
- 本来就干净的 UV → 不动（applied=False）；
- 两片 UV 完全同心的病态布局 → 岛级变换原理上不可分，如实上报 remaining。
"""

from __future__ import annotations

import numpy as np
import trimesh

from app.tools.uv import deoverlap_uv, uv_overlap


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


def test_deoverlap_resolves_overlapping_islands_with_distinct_centroids():
    # 三角 B 从上方压进 A 的上尖角 —— 岛心错开、局部交叠，典型的 packing 挤压形态
    uv_a = [(0.1, 0.1), (0.5, 0.1), (0.1, 0.5)]
    uv_b = [(0.02, 0.25), (0.62, 0.25), (0.02, 0.85)]
    mesh = _two_triangle_mesh(uv_a, uv_b)

    before = uv_overlap(mesh)
    assert before["computable"] and before["count"] > 0, "测试前提：构造出重叠"

    mesh, report = deoverlap_uv(mesh)

    assert report["applied"] is True
    assert report["remaining"] == 0, report
    after = uv_overlap(mesh)
    assert after["count"] == 0
    # UV 仍在一个合理范围内（收缩 + 外推不应该把岛推出去）
    uv = mesh.visual.uv
    assert uv.min() >= -0.5 and uv.max() <= 1.5


def test_deoverlap_is_noop_on_clean_uv():
    uv_a = [(0.0, 0.0), (0.3, 0.0), (0.0, 0.3)]
    uv_b = [(0.6, 0.6), (0.9, 0.6), (0.6, 0.9)]
    mesh = _two_triangle_mesh(uv_a, uv_b)
    original = mesh.visual.uv.copy()

    mesh, report = deoverlap_uv(mesh)

    assert report["applied"] is False
    assert report["iterations"] == 0
    assert report["remaining"] == 0
    assert np.allclose(mesh.visual.uv, original)


def test_deoverlap_reports_remaining_for_nested_islands():
    # 小岛完全落在小岛内部：纯"向质心收缩"的岛级变换原理上不可分
    #（收缩后小岛仍在岛内），如实报 remaining，交校验规则处置
    uv_a = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    uv_b = [(0.3, 0.3), (0.5, 0.3), (0.3, 0.5)]
    mesh = _two_triangle_mesh(uv_a, uv_b)

    before = uv_overlap(mesh)
    assert before["count"] > 0, "测试前提：嵌套构造出重叠"

    mesh, report = deoverlap_uv(mesh)

    assert report["applied"] is True  # 确实尝试过收缩
    assert report["remaining"] is not None and report["remaining"] > 0


def test_deoverlap_without_uv_is_a_noop():
    mesh = trimesh.creation.box()

    mesh, report = deoverlap_uv(mesh)

    assert report["applied"] is False
    assert "没有 UV" in report.get("reason", "")

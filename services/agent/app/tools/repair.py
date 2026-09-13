"""管线步骤 2：网格修复。

生成模型常见的四种病：退化面、游离小组件、法线不统一、破洞。
顺序有讲究 —— 先清退化面再去组件，最后补洞，否则补洞会把垃圾一起缝进去。
"""

from __future__ import annotations

import contextlib
from typing import Any

import numpy as np
import trimesh

from .mesh_io import mesh_stats, welded_face_components

# 小于主体这个比例的组件视为"垃圾"，直接剔除（还要同时满足"远离主体"才删）
COMPONENT_FACE_RATIO = 0.10
COMPONENT_VOLUME_RATIO = 0.01
# 主体包围盒按自身对角线的这个比例向外膨胀；膨胀盒内的小组件一律保留
MAIN_BBOX_INFLATE = 0.05


def _bbox_overlap(a_low, a_high, b_low, b_high) -> bool:
    return bool(np.all(a_low <= b_high) and np.all(b_low <= a_high))


def _drop_stray_components(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, int, str | None]:
    """只剔除"又小、又小、又远离主体"的游离垃圾，返回 (mesh, 剔除数量, 失败原因)。

    组件划分用 welded_face_components（UV 岛不是组件 —— 2026-09-13 摩托复盘：
    旧实现按 face_adjacency 找组件，glTF 的 UV 缝复制顶点把整车拆成 1.7 万个
    "岛"，被当垃圾删掉 9.9 万个面）。剔除必须**同时满足**：面数 < 主体的 10%、
    包围盒对角线 < 主体的 21.5%、且不与主体膨胀包围盒（对角线 ×
    MAIN_BBOX_INFLATE）相交。第三个条件是摩托复盘加上的：装配体（整车 979
    零件）的螺丝、后视镜、徽标又小又少面，但都嵌在车体附近 —— 它们是结构，
    不是垃圾；飘在主体旁边的碎片（mushroom_house 实测偏移 0.9 的方块）才是垃圾。
    """
    try:
        components = welded_face_components(mesh)
    except Exception as exc:
        return mesh, 0, f"{type(exc).__name__}: {str(exc)[:120]}"
    if len(components) <= 1:
        return mesh, 0, None

    main = max(components, key=len)
    main_faces = max(1, len(main))
    main_points = mesh.vertices[np.unique(mesh.faces[main])]
    main_low, main_high = main_points.min(axis=0), main_points.max(axis=0)
    main_extent = float(np.linalg.norm(main_high - main_low))
    inflate = main_extent * MAIN_BBOX_INFLATE

    keep = np.zeros(len(mesh.faces), dtype=bool)
    keep[main] = True
    dropped = 0
    for component in components:
        if component is main or len(component) == 0:
            continue
        if len(component) / main_faces >= COMPONENT_FACE_RATIO:
            keep[component] = True
            continue
        points = mesh.vertices[np.unique(mesh.faces[component])]
        extent = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
        if main_extent > 0 and extent / main_extent >= COMPONENT_VOLUME_RATIO ** (1 / 3):
            keep[component] = True
            continue
        if _bbox_overlap(main_low - inflate, main_high + inflate, points.min(axis=0), points.max(axis=0)):
            keep[component] = True  # 贴着主体的小零件：保留
            continue
        dropped += 1

    if dropped == 0:
        return mesh, 0, None
    working = mesh.copy()
    working.update_faces(keep)
    working.remove_unreferenced_vertices()
    return working, dropped, None


def _safe_volume(mesh: trimesh.Trimesh) -> float:
    """非封闭网格算不出体积 —— 返回 0 而不是抛异常，让剔除逻辑退回按面数判断。"""
    if not mesh.is_watertight:
        return 0.0
    try:
        return abs(float(mesh.volume))
    except Exception:
        return 0.0


def repair_mesh(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """修复网格。返回 (修复后的网格, 报告)。"""
    report: dict[str, Any] = {"before": mesh_stats(mesh), "actions": []}
    working = mesh.copy()

    # 1) 退化面：零面积 / 重复顶点，留着会让后续 UV 和烘焙出莫名其妙的错。
    #    trimesh 5.x 起 remove_* 系列换成了"返回掩码 + update_faces"的写法。
    before = len(working.faces)
    working.update_faces(working.nondegenerate_faces())
    removed_degenerate = before - len(working.faces)
    if removed_degenerate:
        report["actions"].append(f"清除退化面 {removed_degenerate} 个")

    # 2) 重复面
    before = len(working.faces)
    working.update_faces(working.unique_faces())
    removed_duplicate = before - len(working.faces)
    if removed_duplicate:
        report["actions"].append(f"清除重复面 {removed_duplicate} 个")

    # 3) 游离组件（生成模型的经典病：旁边飘着一小块）
    working, dropped, split_failure = _drop_stray_components(working)
    if dropped:
        report["actions"].append(f"剔除游离组件 {dropped} 个")
    if split_failure:
        report["actions"].append(f"组件分析跳过（{split_failure}）")

    # 4) 未引用顶点
    before = len(working.vertices)
    working.remove_unreferenced_vertices()
    removed_vertices = before - len(working.vertices)
    if removed_vertices:
        report["actions"].append(f"清理未引用顶点 {removed_vertices} 个")

    # 5) 法线统一（修复步骤里最影响观感的一项）
    try:
        trimesh.repair.fix_normals(working, multibody=True)
        report["actions"].append("统一法线朝向")
    except Exception as exc:
        report["actions"].append(f"法线统一跳过：{exc}")

    # 6) 补洞
    holes_before = _hole_count(working)
    try:
        trimesh.repair.fill_holes(working)
        holes_after = _hole_count(working)
        if holes_before != holes_after:
            report["actions"].append(f"补洞 {holes_before - holes_after} 处")
    except Exception as exc:
        report["actions"].append(f"补洞跳过：{exc}")

    with contextlib.suppress(Exception):
        trimesh.repair.fix_inversion(working)

    report["after"] = mesh_stats(working)
    report["changed"] = bool(report["actions"])
    return working, report


def _hole_count(mesh: trimesh.Trimesh) -> int:
    """用欧拉特征近似统计洞的数量；算不出来返回 0。"""
    try:
        return max(0, int(2 - mesh.euler_number))
    except Exception:
        return 0


def normalize_transform(
    mesh: trimesh.Trimesh,
    pivot: str = "bottom_center",
    expected_size_m: float | None = None,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """归一化摆放与尺度 —— 把"轴心/单位"两条校验规则变成管线自己保证的事。

    这一步不做的话，"轴心在底面中心""米制 ±5%"这两条规则在生成资产上几乎必然 FAIL，
    而美术要的是能直接进引擎的东西，不是一份告诉他哪里不对的报告。
    """
    actions: list[str] = []
    working = mesh.copy()
    low, high = working.bounds
    extents = np.asarray(high - low, dtype=np.float64)

    # 1) 单位归一：把最长边缩放到规格期望的尺寸
    scaled = False
    if expected_size_m and expected_size_m > 0:
        longest = float(np.max(extents))
        if longest > 1e-9:
            factor = float(expected_size_m) / longest
            if abs(factor - 1.0) > 1e-4:
                working.apply_scale(factor)
                actions.append(f"按规格缩放 ×{factor:.4f} 至最长边 {expected_size_m}m")
                scaled = True
                low, high = working.bounds
                extents = np.asarray(high - low, dtype=np.float64)

    # 2) 轴心归一（管线内部统一 Y-up —— GLB/three.js 的世界约定）
    if pivot == "bottom_center":
        low, high = working.bounds
        center = (np.asarray(low) + np.asarray(high)) / 2.0
        offset = np.array([center[0], low[1], center[2]])
        if float(np.max(np.abs(offset))) > 1e-6:
            working.apply_translation(-offset)
            actions.append("轴心移到包围盒底面中心")
    elif pivot == "origin":
        low, high = working.bounds
        center = (np.asarray(low) + np.asarray(high)) / 2.0
        if float(np.max(np.abs(center))) > 1e-6:
            working.apply_translation(-center)
            actions.append("轴心移到几何中心（origin 模式）")

    return working, {"actions": actions, "scaled": scaled, "pivot": pivot}


__all__ = [
    "COMPONENT_FACE_RATIO",
    "COMPONENT_VOLUME_RATIO",
    "normalize_transform",
    "repair_mesh",
]

"""管线步骤 4：UV 展开，以及 UV 相关的分析（岛划分、重叠检测、岛间距）。

UV 是"美术会不会用这个资产"的分水岭之一，所以这里的分析必须**能定位** ——
只说"UV 有重叠"没用，得说出是哪几个面，前端才能高亮。

展开后端用 xatlas（纯 pip，跨平台）。装不上就跳过并说明原因，不阻断管线。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import trimesh

from .mesh_io import uv_array

# 岛间距计算的成本上限，超过就放弃计算（返回 None 而不是给个假数字）
MAX_BOUNDARY_SEGMENTS = 20000


# ------------------------------------------------------------------ 展开


def unwrap(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """UV 展开。返回 (带 UV 的网格, 报告)。

    已知局限：xatlas 的 Python 绑定只接受 (positions, indices, normals, uvs)，
    **不暴露 chart / packing 的 padding 参数**，所以管线暂时无法保证"零 UV 重叠"
    （实测约 0.04% 的面会有微小重叠）。这不是可以靠调参绕过的，M3 需要换展开器
    （Blender Smart UV Project）或补一步去重叠后处理。在此之前校验器把 UV 重叠
    报成 WARN 而不是 FAIL，见 recipes/validation_rules.yaml。
    """
    report: dict[str, Any] = {"method": "", "skipped_reason": None}
    try:
        import xatlas  # type: ignore
    except ImportError:
        report["skipped_reason"] = (
            "未安装 UV 展开后端。请执行 `pip install -e \".[mesh]\"` 安装 xatlas 后重试。"
        )
        return mesh, report

    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.uint32)
    try:
        vmapping, indices, uvs = xatlas.parametrize(vertices, faces)
    except Exception as exc:
        report["skipped_reason"] = f"UV 展开失败（xatlas）：{exc}"
        return mesh, report

    new_vertices = np.asarray(vertices, dtype=np.float64)[np.asarray(vmapping, dtype=np.int64)]
    new_faces = np.asarray(indices, dtype=np.int64)
    new_uvs = np.asarray(uvs, dtype=np.float64)

    unwrapped = trimesh.Trimesh(vertices=new_vertices, faces=new_faces, process=False)
    try:
        unwrapped.visual = trimesh.visual.TextureVisuals(uv=new_uvs)
    except Exception as exc:
        report["skipped_reason"] = f"写入 UV 失败：{exc}"
        return mesh, report

    report["method"] = "xatlas"
    report["uv_islands"] = len(uv_islands(unwrapped))
    report["vertices"] = int(len(new_vertices))
    report["faces"] = int(len(new_faces))
    return unwrapped, report


# ------------------------------------------------------------------ 去重叠后处理


# 每轮收缩系数，从轻到重：先试探 1%，解决不了再加码。
# 收缩会等比放大岛间距，等效于给 xatlas 的 packing 补上缺失的 padding。
_SHRINK_SCHEDULE = (0.99, 0.985, 0.98, 0.97, 0.96, 0.95, 0.93, 0.90)


def deoverlap_uv(
    mesh: trimesh.Trimesh,
    schedule: tuple[float, ...] = _SHRINK_SCHEDULE,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """去重叠后处理：把卷入重叠的 UV 岛向自身质心逐轮收缩，直到检测器报零。

    xatlas 的 Python 绑定不暴露 packing 的 padding 参数，岛与岛之间偶尔贴得太近。
    把**只卷入重叠**的岛向质心收缩一个小系数、同时把岛心略微推离重叠群中心，
    等效于补上间距，而 UV 变形被限制在最小范围（正常岛一动不动）。

    收缩到 uv_overlap() 报零为止；到系数上限仍剩重叠（如两岛 UV 完全同心的
    病态布局，纯岛级变换原理上不可分）时如实返回 remaining，交校验规则处置。
    """
    uv = uv_array(mesh)
    report: dict[str, Any] = {
        "applied": False,
        "iterations": 0,
        "scales": [],
        "remaining": None,
        "remaining_ratio": None,
    }
    if uv is None:
        report["reason"] = "该网格没有 UV，无需去重叠"
        return mesh, report

    faces = np.asarray(mesh.faces, dtype=np.int64)

    for factor in schedule:
        overlap = uv_overlap(mesh)
        if not overlap.get("computable"):
            report["reason"] = overlap.get("reason", "重叠不可检测")
            return mesh, report

        count = int(overlap.get("count", 0))
        if count == 0:
            report["remaining"] = 0
            report["remaining_ratio"] = 0.0
            return mesh, report

        overlap_faces = set(overlap.get("overlap_faces", []))
        islands = uv_islands(mesh)
        affected = [island for island in islands if overlap_faces.intersection(island.tolist())]
        if not affected:
            # 检测器报了重叠却归不出岛（理论上的边界情况）：不硬来，如实上报
            report["remaining"] = count
            report["remaining_ratio"] = overlap.get("ratio")
            report["reason"] = "重叠面无法归属到任何 UV 岛"
            return mesh, report

        # 岛心沿"重叠群中心 → 自身"的方向略微外推：收缩腾出的空间不会被
        # 相邻岛立刻吃回去，同心布局也多一层分离机会
        centroids = []
        for island in affected:
            vertex_ids = np.unique(faces[island])
            centroids.append(uv[vertex_ids].mean(axis=0))
        center = np.mean(np.asarray(centroids), axis=0)

        for island, centroid in zip(affected, centroids):
            vertex_ids = np.unique(faces[island])
            pushed = center + (centroid - center) * (1.0 + (1.0 - factor))
            uv[vertex_ids] = pushed + (uv[vertex_ids] - centroid) * factor

        if not isinstance(mesh.visual, trimesh.visual.texture.TextureVisuals):
            mesh.visual = trimesh.visual.TextureVisuals(uv=uv)
        else:
            mesh.visual.uv = uv
        report["applied"] = True
        report["iterations"] += 1
        report["scales"].append(factor)

    # 系数用尽：如实报告剩余重叠（校验规则会按 FAIL 拦导出，由美术决定下一步）
    overlap = uv_overlap(mesh)
    if overlap.get("computable"):
        report["remaining"] = int(overlap.get("count", 0))
        report["remaining_ratio"] = overlap.get("ratio")
    return mesh, report


# ------------------------------------------------------------------ 分析


def uv_face_coords(mesh: trimesh.Trimesh) -> np.ndarray | None:
    """返回 (F, 3, 2) 的逐面 UV 三角形坐标。没有 UV 返回 None。"""
    uv = uv_array(mesh)
    if uv is None:
        return None
    return uv[np.asarray(mesh.faces, dtype=np.int64)]


def uv_islands(mesh: trimesh.Trimesh, tolerance: float = 1e-6) -> list[np.ndarray]:
    """把面按 UV 连通性分岛。返回每岛的面索引数组列表。"""
    coords = uv_face_coords(mesh)
    if coords is None:
        return []

    flat = coords.reshape(-1, 2)
    quantized = np.round(flat / tolerance).astype(np.int64)
    _, inverse = np.unique(quantized, axis=0, return_inverse=True)
    uv_ids = inverse.reshape(-1, 3)

    parent = np.arange(int(uv_ids.max()) + 1)

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for tri in uv_ids:
        union(int(tri[0]), int(tri[1]))
        union(int(tri[1]), int(tri[2]))

    labels = np.array([find(int(t)) for t in uv_ids[:, 0]])
    islands: dict[int, list[int]] = {}
    for face_index, label in enumerate(labels):
        islands.setdefault(int(label), []).append(face_index)
    return [np.asarray(v, dtype=np.int64) for v in islands.values()]


def uv_overlap(mesh: trimesh.Trimesh, samples_per_face: int = 4) -> dict[str, Any]:
    """检测 UV 重叠。

    做法：在每个 UV 三角形内部取若干**内点**（不会被相邻面共享），
    再用网格加速判断这个内点是否落在另一个"不相邻"的三角形内部。
    只用共享边来判定会误报 —— 相邻面在 UV 上共享边是完全正常的。
    """
    coords = uv_face_coords(mesh)
    if coords is None:
        return {"computable": False, "reason": "该资产没有 UV，无法检测重叠", "faces": []}

    face_count = len(coords)
    if face_count == 0:
        return {"computable": True, "overlap_faces": [], "ratio": 0.0}

    # 重心方向的内点：从每个顶点向重心收，保证落在三角形内部且远离边界。
    # 用"内点"而不是"采样整个三角形"，是为了避开相邻面共享边造成的误报 ——
    # 相邻面在 UV 上共享边是完全正常的，只有内部真正交叠才算重叠。
    centroids = coords.mean(axis=1)[:, None, :]  # (F, 1, 2)，留一维便于广播
    alphas = np.linspace(0.55, 0.9, max(1, samples_per_face - 1))
    samples = [centroids]
    for alpha in alphas:
        samples.append(coords + (centroids - coords) * alpha)
    points = np.concatenate(samples, axis=1)  # (F, K, 2)

    # 网格加速：按三角形包围盒建桶
    low = coords.min(axis=1)
    high = coords.max(axis=1)
    span = np.maximum(high - low, 1e-9)
    cell = float(np.median(span)) or 1e-6
    grid: dict[tuple[int, int], list[int]] = {}
    for index in range(face_count):
        x0, y0 = np.floor(low[index] / cell).astype(int)
        x1, y1 = np.floor(high[index] / cell).astype(int)
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                grid.setdefault((gx, gy), []).append(index)

    # 网格顶点索引在这里没用：UV 展开会在 UV 缝处复制顶点，缝两侧的面在网格上
    # 不共享顶点索引，但在 UV 空间里共享边 —— 按网格索引判断"相邻"会把正常的
    # UV 缝误报成重叠。所以按 **UV 坐标**给顶点重新编号，用它来判断相邻。
    quantized = np.round(coords.reshape(-1, 2) / 1e-6).astype(np.int64)
    _, inverse = np.unique(quantized, axis=0, return_inverse=True)
    uv_ids = inverse.reshape(-1, 3)

    overlap_faces: set[int] = set()

    for index in range(face_count):
        own = {int(v) for v in uv_ids[index]}
        for point in points[index]:
            key = (int(np.floor(point[0] / cell)), int(np.floor(point[1] / cell)))
            for candidate in grid.get(key, ()):
                if candidate == index or candidate in overlap_faces:
                    continue
                if own & {int(v) for v in uv_ids[candidate]}:
                    continue  # UV 空间里相邻（含 UV 缝两侧），不算重叠
                if _point_in_triangle(point, coords[candidate], margin=1e-6):
                    overlap_faces.add(index)
                    overlap_faces.add(candidate)
                    break
            if index in overlap_faces:
                break

    faces = sorted(overlap_faces)
    return {
        "computable": True,
        "overlap_faces": faces,
        "count": len(faces),
        "ratio": round(len(faces) / max(1, face_count), 4),
    }


def _point_in_triangle(point: np.ndarray, tri: np.ndarray, margin: float = 0.0) -> bool:
    """重心坐标法判断点在三角形内。

    margin 给一个正数时要求点**严格**落在内部（重心坐标都大于 margin），
    避免"点恰好压在边上"被算成重叠 —— 浮点误差会让这种情况经常发生，
    而共享边的两个三角形是合法的，不该报重叠。
    """
    (x1, y1), (x2, y2), (x3, y3) = tri
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(denominator) < 1e-12:
        return False
    a = ((y2 - y3) * (point[0] - x3) + (x3 - x2) * (point[1] - y3)) / denominator
    b = ((y3 - y1) * (point[0] - x3) + (x1 - x3) * (point[1] - y3)) / denominator
    c = 1.0 - a - b
    return a >= margin and b >= margin and c >= margin


def uv_island_margin_px(mesh: trimesh.Trimesh, resolution: int) -> float | None:
    """UV 岛之间的最小间距，换算成像素。

    用岛边界线段之间的最小距离来算 —— 比"包围盒间距"准确得多，
    包围盒会把 L 形岛的凹口也算成间距，从而给出偏乐观的结论。
    算不动时返回 None（表示"测不了"），而不是返回一个假数字。
    """
    coords = uv_face_coords(mesh)
    islands = uv_islands(mesh)
    if coords is None or len(islands) < 2:
        return None

    segments_per_island: list[np.ndarray] = []
    for island in islands:
        boundary = _boundary_segments(coords, island)
        if len(boundary):
            segments_per_island.append(boundary)

    total = sum(len(s) for s in segments_per_island)
    if len(segments_per_island) < 2 or total > MAX_BOUNDARY_SEGMENTS:
        return None

    best: float | None = None
    for i in range(len(segments_per_island)):
        for j in range(i + 1, len(segments_per_island)):
            distance = _segments_min_distance(segments_per_island[i], segments_per_island[j])
            if distance is not None and (best is None or distance < best):
                best = distance
    if best is None:
        return None
    return round(float(best) * resolution, 3)


def _boundary_segments(coords: np.ndarray, island: np.ndarray) -> np.ndarray:
    """提取某岛在 UV 空间中的边界线段（只被一个面用到的边）。"""
    flat = coords[island].reshape(-1, 2)
    quantized = np.round(flat * 1e6).astype(np.int64)
    unique, inverse = np.unique(quantized, axis=0, return_inverse=True)
    tri_ids = inverse.reshape(-1, 3)
    coord_of = unique.astype(np.float64) / 1e6

    usage: dict[tuple[int, int], int] = {}
    for tri in tri_ids:
        for a in range(3):
            b = (a + 1) % 3
            key = (min(int(tri[a]), int(tri[b])), max(int(tri[a]), int(tri[b])))
            usage[key] = usage.get(key, 0) + 1

    segments = [coord_of[[k[0], k[1]]] for k, count in usage.items() if count == 1]
    if not segments:
        return np.zeros((0, 2, 2))
    return np.asarray(segments, dtype=np.float64)


def _segments_min_distance(left: np.ndarray, right: np.ndarray) -> float | None:
    """两组线段之间的最小距离（向量化，控制内存）。"""
    if len(left) == 0 or len(right) == 0:
        return None
    best = float("inf")
    chunk = 256
    for start in range(0, len(left), chunk):
        block = left[start : start + chunk]  # (B, 2, 2)
        p1 = block[:, 0, :][:, None, :]
        p2 = block[:, 1, :][:, None, :]
        q1 = right[None, :, 0, :]
        q2 = right[None, :, 1, :]
        distances = _segment_pair_distance(p1, p2, q1, q2)
        local = float(np.min(distances))
        if local < best:
            best = local
    return None if best == float("inf") else best


def _segment_pair_distance(p1: np.ndarray, p2: np.ndarray, q1: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """线段对之间的距离（2D）。用采样 + 端点距离近似，够用于间距判定。"""
    # 端点-线段距离（双向），再取 4 个端点距离的最小值
    d = np.minimum.reduce(
        [
            _point_segment_distance(p1, q1, q2),
            _point_segment_distance(p2, q1, q2),
            _point_segment_distance(q1, p1, p2),
            _point_segment_distance(q2, p1, p2),
            np.linalg.norm(p1 - q1, axis=-1),
            np.linalg.norm(p2 - q2, axis=-1),
        ]
    )
    return d


def _point_segment_distance(point: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    denominator = np.sum(ab * ab, axis=-1)
    denominator = np.where(denominator < 1e-18, 1e-18, denominator)
    t = np.sum((point - a) * ab, axis=-1) / denominator
    t = np.clip(t, 0.0, 1.0)
    projection = a + t[..., None] * ab
    return np.linalg.norm(point - projection, axis=-1)


__all__ = [
    "MAX_BOUNDARY_SEGMENTS",
    "deoverlap_uv",
    "unwrap",
    "uv_face_coords",
    "uv_island_margin_px",
    "uv_islands",
    "uv_overlap",
]

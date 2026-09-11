"""网格读写与体检。

这一层是所有管线步骤的共同地基：加载、统计、几何属性查询。
刻意不做任何"修改"操作 —— 修改都在各自的步骤模块里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import trimesh

# trimesh 能直接读的扩展名
SUPPORTED_EXTENSIONS = {".glb", ".gltf", ".obj", ".ply", ".stl", ".off", ".3mf"}

# mesh.split() 在超大网格上很贵，超过这个面数就不做组件分析
COMPONENT_ANALYSIS_LIMIT = 300_000


class MeshError(RuntimeError):
    """网格层面的可预期错误，消息会透给前端。"""


def load_mesh(path: Path | str, process: bool = False) -> trimesh.Trimesh:
    """加载网格。

    process=False 是刻意的：我们要**如实**看到上游给的脏数据（退化面、游离组件、法线混乱），
    否则修复步骤就变成了自欺欺人 —— trimesh 默认会顺手清掉一部分问题。
    """
    path = Path(path)
    if not path.exists():
        raise MeshError(f"文件不存在：{path}")
    if path.suffix.lower() == ".fbx":
        raise MeshError(
            "FBX 应在「导入模型」入口上传，会自动经 Blender 转成 GLB 工作副本。"
            "请重新从工作台的导入入口上传。"
        )
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MeshError(
            f"暂不支持 {path.suffix} 格式。支持：{'、'.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    try:
        loaded = trimesh.load(path, force="mesh", process=process)
    except Exception as exc:
        raise MeshError(f"读取网格失败：{exc}") from exc

    if isinstance(loaded, trimesh.Scene):
        geometries = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise MeshError("文件里没有可用的网格几何体。")
        loaded = trimesh.util.concatenate(geometries) if len(geometries) > 1 else geometries[0]

    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise MeshError("文件里没有可用的三角面。")
    return loaded


def save_mesh(mesh: trimesh.Trimesh, path: Path | str) -> Path:
    """按扩展名导出。GLB 优先（预览格式统一 GLB，前端不引重 loader）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    file_type = {
        ".glb": "glb",
        ".gltf": "gltf",
        ".obj": "obj",
        ".ply": "ply",
        ".stl": "stl",
    }.get(suffix)
    if file_type is None:
        raise MeshError(f"不支持导出为 {suffix}")
    try:
        data = mesh.export(file_type=file_type)
    except Exception as exc:
        raise MeshError(f"导出 {suffix} 失败：{exc}") from exc
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


# ------------------------------------------------------------------ 几何属性


def uv_array(mesh: trimesh.Trimesh) -> np.ndarray | None:
    """取逐顶点的 UV。trimesh 把 UV 放在 visual.uv，长度应与顶点数一致。"""
    visual = getattr(mesh, "visual", None)
    uv = getattr(visual, "uv", None)
    if uv is None:
        return None
    uv = np.asarray(uv, dtype=np.float64)
    if uv.ndim != 2 or uv.shape[0] != len(mesh.vertices):
        return None
    return uv


def face_normals(mesh: trimesh.Trimesh) -> np.ndarray:
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    if normals.ndim != 2 or len(normals) != len(mesh.faces):
        normals = np.zeros((len(mesh.faces), 3), dtype=np.float64)
    return normals


def component_count(mesh: trimesh.Trimesh) -> int:
    """几何壳体数量（连通分量）。游离小组件是生成模型的常见病。

    两个坑，都实际踩过：

    1. **不用 mesh.split()**：它会对每个分量尝试补洞（内部依赖 networkx，代价高），
       给一个只读统计带来"顺手改了几何"的副作用。这里只要一个数字。
    2. **必须先按位置焊接顶点**：UV 展开（xatlas）会在 UV 缝处复制顶点，而面邻接是
       按顶点索引算的。不焊接的话，一个几何上完整的道具会被报成"有 6 个组件" ——
       那其实是 UV 岛的个数。美术看到这个数字会以为模型坏了。
    """
    if len(mesh.faces) > COMPONENT_ANALYSIS_LIMIT:
        return -1  # -1 = 太大没算
    try:
        from trimesh.graph import connected_components

        welded = trimesh.Trimesh(
            vertices=np.asarray(mesh.vertices, dtype=np.float64),
            faces=np.asarray(mesh.faces, dtype=np.int64),
            process=True,  # process 会合并重合顶点，把 UV 缝重新焊上
        )
        labels = connected_components(
            welded.face_adjacency, nodes=np.arange(len(welded.faces), dtype=np.int64)
        )
        return len(labels)
    except Exception:
        return -1


def bounds(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    return np.asarray(mesh.bounds[0], dtype=np.float64), np.asarray(mesh.bounds[1], dtype=np.float64)


def mesh_stats(mesh: trimesh.Trimesh) -> dict[str, Any]:
    """体检报告。这些数字既给校验器用，也直接展示在资产详情页。"""
    low, high = bounds(mesh)
    extents = high - low
    stats: dict[str, Any] = {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "triangles": int(len(mesh.faces)),
        "extents": [round(float(v), 5) for v in extents],
        "longest_edge_m": round(float(np.max(extents)), 5),
        "bounds_min": [round(float(v), 5) for v in low],
        "bounds_max": [round(float(v), 5) for v in high],
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "components": component_count(mesh),
        "has_uv": uv_array(mesh) is not None,
    }
    return stats


# ------------------------------------------------------------------ 四边面


def quad_ratio_from_file(path: Path | str) -> float | None:
    """从**原始文件**统计四边面比例。

    必须读原始文件而不是 trimesh 加载后的结果：trimesh 会把所有面三角化，
    加载之后四边面信息就永久丢失了。目前只有 OBJ 能可靠还原（`f a b c d`）。
    其它格式返回 None，表示"测不了"，而不是"不合格"。
    """
    path = Path(path)
    if path.suffix.lower() != ".obj":
        return None
    quads = 0
    triangles = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.startswith("f "):
                    continue
                arity = len(line.split()) - 1
                if arity == 4:
                    quads += 1
                elif arity == 3:
                    triangles += 1
                elif arity > 4:
                    triangles += arity - 2  # n-gon 按三角化后的面数计
    except OSError:
        return None
    total = quads + triangles
    if total == 0:
        return None
    return round(quads / total, 4)


def ngon_faces_from_file(path: Path | str) -> list[int] | None:
    """n-gon（顶点数 > 4 的面）的面索引。同样只有 OBJ 能可靠还原。

    索引对应 OBJ 中 `f` 行的顺序。因为我们加载时 process=False、导出 GLB 时也不重排，
    所以这个索引与视口里的三角面索引一致，可以直接用来高亮。
    """
    path = Path(path)
    if path.suffix.lower() != ".obj":
        return None
    indices: list[int] = []
    face_index = -1
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.startswith("f "):
                    continue
                face_index += 1
                if len(line.split()) - 1 > 4:
                    indices.append(face_index)
    except OSError:
        return None
    return indices


def ngon_count_from_file(path: Path | str) -> int | None:
    """统计 n-gon（顶点数 > 4 的面）。同样只有 OBJ 能可靠还原。"""
    faces = ngon_faces_from_file(path)
    return None if faces is None else len(faces)


__all__ = [
    "COMPONENT_ANALYSIS_LIMIT",
    "SUPPORTED_EXTENSIONS",
    "MeshError",
    "bounds",
    "component_count",
    "face_normals",
    "load_mesh",
    "mesh_stats",
    "ngon_count_from_file",
    "ngon_faces_from_file",
    "quad_ratio_from_file",
    "save_mesh",
    "uv_array",
]

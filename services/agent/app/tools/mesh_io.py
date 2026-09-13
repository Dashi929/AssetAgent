"""网格读写与体检。

这一层是所有管线步骤的共同地基：加载、统计、几何属性查询。
刻意不做任何"修改"操作 —— 修改都在各自的步骤模块里。
"""

from __future__ import annotations

import contextlib
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

    .glb/.gltf 走自有解析器（app/tools/gltf_load.py）：trimesh 的 GLTF 节点变换解析
    对「matrix 挂在父节点」的层级会整体错位（实测 Sketchfab 导出 26/26 节点全错），
    这是导入渲染"炸裂"的根因，不能让它碰 GLB。
    """
    path = Path(path)
    if not path.exists():
        raise MeshError(f"文件不存在：{path}")
    if path.suffix.lower() == ".fbx":
        raise MeshError(
            "FBX 应在「导入模型」入口上传，会自动转换为 GLB 工作副本。"
            "请重新从工作台的导入入口上传。"
        )
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MeshError(
            f"暂不支持 {path.suffix} 格式。支持：{'、'.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if path.suffix.lower() in {".glb", ".gltf"}:
        from .gltf_load import load_glb

        loaded = load_glb(path)
        if len(loaded.faces) == 0:
            raise MeshError("文件里没有可用的三角面。")
        return loaded

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

    if suffix in {".glb", ".gltf"}:
        _sanitize_for_export(mesh)

    try:
        data = mesh.export(file_type=file_type)
    except Exception as exc:
        raise MeshError(f"导出 {suffix} 失败：{exc}") from exc
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def _sanitize_for_export(mesh: trimesh.Trimesh) -> None:
    """GLB 导出前的兜底清理（三件事都是 mushroom_house 实测踩出来的）：

    1. 绕序统一：减面会把部分面的绕序翻反，singleSided 导出下这些面被
       three.js 剔除，视口呈现大片黑色"内壁"；
    2. 材质兜底：只有 UV 没有材质的网格，trimesh 会自动塞一张 2×2 灰色
       占位贴图 × 0.4 底色系数，渲染出来近乎纯黑 —— 换成中性浅灰双面
       clay 材质（有 UV 带 UV，没 UV 也可以只挂材质）；
    3. 顶点法线清掉：处理链上的顶点法线可能指向混乱，让 GLTFLoader 自己按
       面绕序重算（three.js 对无 NORMAL 的网格会 computeVertexNormals）。
    """
    with contextlib.suppress(Exception):
        trimesh.repair.fix_normals(mesh, multibody=True)
    _orient_components_outward(mesh)


def _orient_components_outward(mesh: trimesh.Trimesh) -> None:
    """按连通组件校正法线朝向（fix_normals 的兜底）。

    fix_normals 用体积判定朝外，对几千个非水密小组件（生成模型的装饰碎件）
    大面积判反（实测 73% 朝内，视口因背面剔除渲染成黑壳）。这里用轻量判据：
    组件内 Σ(面法线 · (面心-组件质心)) < 0 视为朝内，翻转该组件全部面的绕序。
    对"外凸为主"的道具组件足够可靠；个别真内凹组件由 doubleSided 材质兜底
    （three.js 对背面片元会翻转法线，光照依旧正确）。
    """
    import numpy as np

    with contextlib.suppress(Exception):
        faces = np.asarray(mesh.faces, dtype=np.int64)
        if len(faces) == 0 or len(mesh.face_adjacency) == 0:
            return
        from trimesh.graph import connected_components

        components = connected_components(
            mesh.face_adjacency, nodes=np.arange(len(faces)), min_len=1, engine="scipy"
        )
        face_normals = np.asarray(mesh.face_normals, dtype=np.float64)
        centers = mesh.vertices[faces].mean(axis=1)
        flip = np.zeros(len(faces), dtype=bool)
        for component in components:
            if len(component) < 3:
                continue
            comp_center = centers[component].mean(axis=0)
            dots = np.einsum("ij,ij->i", face_normals[component], centers[component] - comp_center)
            if dots.sum() < 0:
                flip[component] = True
        if flip.any():
            # 注意：update_faces 是"删除掩码"语义，传 (F,3) 数组会被当成
            # fancy index 产出 (F,3,3) 坐标数组（实测）。翻转绕序直接用
            # faces setter 整体赋值：行数不变，UV/视觉属性天然对齐。
            new_faces = faces.copy()
            new_faces[flip] = new_faces[flip][:, [0, 2, 1]]
            mesh.faces = new_faces

    clay = trimesh.visual.material.PBRMaterial(
        baseColorFactor=[0.62, 0.62, 0.62, 1.0],
        roughnessFactor=0.9,
        doubleSided=True,
    )
    visual = mesh.visual
    material = getattr(visual, "material", None)
    has_real_pbr = isinstance(material, trimesh.visual.material.PBRMaterial) and (
        getattr(material, "baseColorTexture", None) is not None
    )
    # 注意：TextureVisuals(uv=...) 构造时 material 会被填成 Material() 基类实例
    # 而不是 None —— 这个基类材质导出 GLB 时会被 trimesh 换成 2×2 灰占位图
    # × 0.4 底色（近黑渲染的来源），所以"没有真 PBR 贴图材质"就一律兜底。
    if isinstance(visual, trimesh.visual.texture.TextureVisuals) and not has_real_pbr:
        uv = getattr(visual, "uv", None)
        if uv is not None and len(uv) == len(mesh.vertices):
            mesh.visual = trimesh.visual.texture.TextureVisuals(uv=uv, material=clay)
        else:
            mesh.visual = trimesh.visual.texture.TextureVisuals(material=clay)
    elif isinstance(visual, trimesh.visual.ColorVisuals):
        mesh.visual = trimesh.visual.texture.TextureVisuals(material=clay)# ------------------------------------------------------------------ 几何属性


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


def welded_face_components(mesh: trimesh.Trimesh) -> list[np.ndarray]:
    """按**顶点位置焊接**后的连通组件（返回面索引分组）。

    glTF 在 UV 缝上**故意复制顶点**（同一位置、不同 UV），直接用
    face_adjacency 或 trimesh 的 process=True 合并找连通分量，都会把每个
    UV 岛当成独立组件（trimesh 5.1.0 实测 process 只合并了部分顶点，
    476 个零件的摩托被报成 27188 个"组件"）。这里只对邻接计算做位置
    焊接，不改动网格本身；顶点坐标逐位相同才合并（glTF 复制的顶点来自
    同一份 buffer 数据，位置严格相等）。
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    _, inverse = np.unique(vertices, axis=0, return_inverse=True)
    welded_faces = inverse[np.asarray(mesh.faces, dtype=np.int64)]
    if len(welded_faces) == 0:
        return []

    tri_edges = np.concatenate(
        [welded_faces[:, [0, 1]], welded_faces[:, [1, 2]], welded_faces[:, [2, 0]]]
    )
    tri_edges.sort(axis=1)
    face_of_edge = np.tile(np.arange(len(welded_faces), dtype=np.int64), 3)
    order = np.lexsort((face_of_edge, tri_edges[:, 1], tri_edges[:, 0]))
    sorted_edges = tri_edges[order]
    sorted_faces = face_of_edge[order]
    same_edge = np.all(sorted_edges[1:] == sorted_edges[:-1], axis=1)
    pairs = np.stack([sorted_faces[:-1][same_edge], sorted_faces[1:][same_edge]], axis=1)
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]  # 退化面焊接后产生自环，去掉
    if len(pairs) == 0:
        return [np.arange(len(welded_faces))]
    return trimesh.graph.connected_components(
        pairs, nodes=np.arange(len(welded_faces)), min_len=1, engine="scipy"
    )


def component_count(mesh: trimesh.Trimesh) -> int:
    """几何壳体数量（连通分量）。游离小组件是生成模型的常见病。

    三个坑，都实际踩过：

    1. **不用 mesh.split()**：它会对每个分量尝试补洞（内部依赖 networkx，代价高），
       给一个只读统计带来"顺手改了几何"的副作用。这里只要一个数字。
    2. **必须先按位置焊接顶点**：UV 展开（xatlas）会在 UV 缝处复制顶点，而面邻接是
       按顶点索引算的。不焊接的话，一个几何上完整的道具会被报成"有 6 个组件" ——
       那其实是 UV 岛的个数。美术看到这个数字会以为模型坏了。
    3. **不能依赖 trimesh 的 process=True 焊接**：5.1.0 实测它对带 UV 的网格只
       合并部分顶点（13.9 万 → 7.6 万，UV 缝仍断开），476 个零件被报成 27188
       个"组件"。用 welded_face_components 按坐标精确焊接。
    """
    if len(mesh.faces) > COMPONENT_ANALYSIS_LIMIT:
        return -1  # -1 = 太大没算
    try:
        return len(welded_face_components(mesh))
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
    "welded_face_components",
]

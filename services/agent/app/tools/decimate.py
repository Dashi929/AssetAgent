"""管线步骤 3：减面 / 重拓扑。

后端优先级：fast-simplification（二次误差边折叠，纯 pip，跨平台）→ pymeshlab → 放弃。
装不上任何后端时**不报错**，而是把这一步标记为 skipped 并说明原因 —— 管线其余部分照跑，
美术仍能拿到一个面数超标的资产，自己判断要不要手动处理。这比整个任务失败有用得多。
"""

from __future__ import annotations

import contextlib
import importlib.util
from typing import Any

import numpy as np
import trimesh

from .mesh_io import mesh_stats


def _backend() -> tuple[str, Any]:
    """探测可用的减面后端。

    用 find_spec 而不是 try-import：真正导入 pymeshlab 要几百毫秒，
    而我们只是想知道它在不在。
    """
    if importlib.util.find_spec("fast_simplification") is not None:
        return "fast-simplification", None
    if importlib.util.find_spec("pymeshlab") is not None:
        return "pymeshlab", None
    return "", None


def decimate_mesh(
    mesh: trimesh.Trimesh,
    target_faces: int,
    want_quads: bool = False,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """减面到 target_faces 以内。返回 (网格, 报告)。

    want_quads 目前只记录意图 —— 自动四边面重拓扑是本项目技术不确定性最高的一环
    （见产品策划文档 5.4.3），需要 W1–W2 的 Spike 结论才决定是否真做。
    """
    target_faces = max(4, int(target_faces))
    before = len(mesh.faces)
    report: dict[str, Any] = {
        "before_faces": before,
        "target_faces": target_faces,
        "want_quads": want_quads,
        "method": "",
        "skipped_reason": None,
    }

    if before <= target_faces:
        report["after_faces"] = before
        report["method"] = "none"
        report["note"] = "面数已在预算内，无需减面"
        return mesh, report

    name, module = _backend()
    if not name:
        report["after_faces"] = before
        report["skipped_reason"] = (
            "未安装减面后端。请执行 `pip install -e \".[mesh]\"` 安装 fast-simplification 后重试。"
        )
        return mesh, report

    try:
        if name == "fast-simplification":
            # 走 trimesh 的封装而不是直接调 fast_simplification.simplify：
            # trimesh 会顺带把 UV / 顶点法线 / 材质映射一起迁移到新拓扑上，
            # 直接调底层库会把这些属性丢掉。
            #
            # 减面前先焊接重合顶点：UV 缝/组件缝上的复制顶点位置完全重合但
            # 索引独立，不焊接的话 quadric 简化会让每份独立塌缩，网格出现
            # 裂纹（"部分顶点对不上"的来源）。管线默认 decimate 之后就是 UV
            # 步（xatlas 从几何重建全部 UV），所以这里合并 UV 缝是安全的；
            # 若单独跑 decimate，UV 会丢（无 UV 网格仍可正确渲染灰模）。
            welded = mesh.copy()
            with contextlib.suppress(Exception):
                welded.merge_vertices(merge_tex=True, merge_norm=True)
            #
            # fast-simplification 是近似收敛：一次调用可能停在预算之上
            # （实测 160k→5000 目标落在 6219）。闭环收紧：按实际比例修正
            # 目标再来一轮，最多五轮，把"面数预算"这条硬承诺兑现。
            simplified = welded
            adjusted = target_faces
            for _ in range(5):
                simplified = _simplify_fast(welded if simplified is welded else simplified, adjusted)
                if len(simplified.faces) <= target_faces or len(simplified.faces) >= len(welded.faces):
                    break
                adjusted = max(4, round(adjusted * target_faces / max(1, len(simplified.faces))))
            if _components_exploded(welded, simplified):
                # 多组件网格（扫描件/带大量装饰小件）整体减面会把小组件炸成
                # 碎片（mushroom_house 实测 26 组件 → 3496 组件）。
                # 回退：逐组件独立减面再合并；组件分析也失败则保留原网格并说明。
                fallback = _decimate_per_component(mesh, target_faces)
                if fallback is None:
                    report["after_faces"] = before
                    report["skipped_reason"] = (
                        "多组件网格整体减面产生碎片，组件级回退也失败，已保留原几何。"
                        "建议在 DCC 里先合并/清理组件后再减面。"
                    )
                    return mesh, report
                simplified = fallback
        else:  # pymeshlab
            import pymeshlab

            mls = pymeshlab.MeshSet()
            mls.add_mesh(pymeshlab.Mesh(vertices=mesh.vertices, faces=mesh.faces))
            mls.meshing_decimation_quadric_edge_collapse(targetfacenum=target_faces)
            out = mls.current_mesh()
            simplified = trimesh.Trimesh(
                vertices=out.vertex_matrix(), faces=out.face_matrix(), process=False
            )
    except Exception as exc:
        import traceback

        report["after_faces"] = before
        report["skipped_reason"] = (
            f"减面失败（{name}）：{exc} || {traceback.format_exc()[-600:]}"
        )
        return mesh, report

    # 减面可能产生新的退化面，顺手清一遍，否则校验器会报一堆噪声
    try:
        simplified.update_faces(simplified.nondegenerate_faces())
        simplified.remove_unreferenced_vertices()
    except Exception:
        pass

    report["after_faces"] = len(simplified.faces)
    report["method"] = name
    report["stats"] = mesh_stats(simplified)
    return simplified, report


def _simplify_fast(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """直接调 fast_simplification（绕开 trimesh 封装里 faces.view 的内存布局坑），
    并强制数组为 C 连续的规范 dtype。UV 等视觉属性不迁移 —— 管线里 decimate
    之后紧跟 UV 步（xatlas 从几何重建全部 UV）。"""
    import fast_simplification

    points = np.ascontiguousarray(mesh.vertices, dtype=np.float64)
    triangles = np.ascontiguousarray(mesh.faces, dtype=np.int32)
    vertices_out, faces_out = fast_simplification.simplify(points, triangles, target_count=target_faces)
    return trimesh.Trimesh(
        vertices=np.asarray(vertices_out, dtype=np.float64),
        faces=np.asarray(faces_out, dtype=np.int64),
        process=False,
    )


def _component_count(mesh: trimesh.Trimesh) -> int:
    """连通组件数（body_count 只算邻接，不构建子网格，便宜）。算不动返回 -1。"""
    try:
        return int(mesh.body_count)
    except Exception:
        return -1


def _components_exploded(before: trimesh.Trimesh, after: trimesh.Trimesh) -> bool:
    """减面后组件数暴涨 = 小组件被炸成碎片（多组件网格的典型失败形态）。"""
    before_count = _component_count(before)
    after_count = _component_count(after)
    if before_count < 0 or after_count < 0:
        return False
    return after_count > max(before_count * 2, 32)


def _decimate_per_component(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh | None:
    """逐组件独立减面再合并（按面数比例分配预算）。失败返回 None。"""
    try:
        parts = mesh.split(only_watertight=False, repair=False)
    except Exception:
        return None
    if not parts:
        return None
    total = sum(len(p.faces) for p in parts)
    if total == 0:
        return None
    decimated: list[trimesh.Trimesh] = []
    for part in parts:
        budget = max(4, round(target_faces * len(part.faces) / total))
        if len(part.faces) <= budget:
            decimated.append(part)
            continue
        try:
            decimated.append(part.simplify_quadric_decimation(face_count=budget))
        except Exception:
            decimated.append(part)  # 单组件失败保留原样，不拖垮整体
    return trimesh.util.concatenate(decimated)


def backend_name() -> str:
    """当前可用的减面后端名，供设置页/诊断接口展示。"""
    name, _ = _backend()
    return name or "未安装"


__all__ = ["backend_name", "decimate_mesh"]

"""管线步骤 3：减面 / 重拓扑。

后端优先级：fast-simplification（二次误差边折叠，纯 pip，跨平台）→ pymeshlab → 放弃。
装不上任何后端时**不报错**，而是把这一步标记为 skipped 并说明原因 —— 管线其余部分照跑，
美术仍能拿到一个面数超标的资产，自己判断要不要手动处理。这比整个任务失败有用得多。
"""

from __future__ import annotations

import importlib.util
from typing import Any

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
            simplified = mesh.simplify_quadric_decimation(face_count=target_faces)
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
        report["after_faces"] = before
        report["skipped_reason"] = f"减面失败（{name}）：{exc}"
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


def backend_name() -> str:
    """当前可用的减面后端名，供设置页/诊断接口展示。"""
    name, _ = _backend()
    return name or "未安装"


__all__ = ["backend_name", "decimate_mesh"]

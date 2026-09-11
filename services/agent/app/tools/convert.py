"""引擎原生格式（FBX）→ GLB 工作副本的导入转换。

设计决策（为什么在导入入口转一次，而不是 load_mesh 里随用随转）：

1. Blender 冷启动数秒，管线 7 步都要读网格；导入时转一次，后续全程 trimesh。
2. 原始 FBX 已经落在 source/ 只读保存，转出的 GLB 放进版本节点目录当工作副本，
   「源文件永不覆盖」的约定不破。
3. FBX 没有纯 Python 的可靠读取方案（trimesh 不支持，assimp 在 Windows 上脆），
   Blender 是项目里已有的、面向美术用户装机率高的转换器 —— 与烘焙/导出同一依赖。

Blender 缺失时明确报错并给出替代路径，而不是静默失败。
"""

from __future__ import annotations

from pathlib import Path

from . import blender
from .mesh_io import MeshError

CONVERT_SCRIPT = "convert_to_glb.py"
CONVERT_TIMEOUT = 300


def convert_to_glb(src: Path, out_dir: Path) -> Path:
    """把 src（当前只支持 FBX）转成 out_dir/<stem>.glb，返回 GLB 路径。

    抛 MeshError（消息透给前端）：Blender 未安装、转换失败、没产出文件。
    """
    src = Path(src)
    out_dir = Path(out_dir)

    if src.suffix.lower() != ".fbx":
        # OBJ/GLB/PLY/STL trimesh 直接读，不该走到这里
        return src

    if not blender.available():
        raise MeshError(
            "导入 FBX 需要 Blender 做格式转换。请安装 Blender（设置页可指定路径），"
            "或先把文件另存为 OBJ / GLB 再导入。"
        )

    target = out_dir / (src.stem + ".glb")
    ok, log = blender.run_script(
        CONVERT_SCRIPT,
        {"input": str(src), "output": str(target)},
        timeout=CONVERT_TIMEOUT,
    )
    if not ok or not target.exists():
        detail = log.strip() or "未知原因"
        raise MeshError(f"FBX 转换为 GLB 失败：{detail}")
    return target


__all__ = ["convert_to_glb"]

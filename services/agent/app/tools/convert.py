"""FBX → GLB 工作副本的导入转换：ufbx 原生优先，Blender 可选回落。

转换链（2026-09-11 起，导入不再依赖 Blender）：

  1. **ufbx 原生转换器**（`native/ufbx2obj`，随应用内置）：exe 产出 OBJ
     （几何 + UV + 烘焙世界变换）→ trimesh 读 OBJ → GLB。无任何外部依赖。
  2. **Blender headless 回落**（可选）：ufbx 解析失败时用另一套解析器再试一次，
     救得回来就救（两者对损坏/异种 FBX 的容错不同）。
  3. 都不可用才报错，消息给替代路径（另存 OBJ / GLB）。

为什么最终工作副本是 GLB：预览格式统一 GLB（见 docs/ARCHITECTURE.md），
前端不引重 loader。原始 FBX 已落 source/ 只读，符合「源文件永不覆盖」约定。

UV 的坑：trimesh 只有在 OBJ 材质**带贴图**时才做 UV 拆分（否则 UV 直接丢），
所以 fbx2obj 会在 OBJ 里写 `mtllib`/`usemtl`，这里配套写一个引用 1×1 白图的
占位 .mtl —— 管线后续会用 xatlas 重展 UV，这张白图只是让导入的 UV 活下来。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import trimesh
from PIL import Image

from .. import paths
from . import blender
from .mesh_io import MeshError, save_mesh

UFBX_TIMEOUT = 120
BLENDER_TIMEOUT = 300


def _convert_with_ufbx(exe: Path, src: Path, out_dir: Path) -> Path:
    obj = out_dir / (src.stem + ".ufbx.obj")
    _run_ufbx2obj(exe, src, obj)

    # 配套占位材质（C 端写的 mtllib 名 = OBJ 全名 + ".mtl"）
    mtl = Path(str(obj) + ".mtl")
    png = out_dir / "assetagent_white.png"
    if not png.exists():
        # pillow 是运行时依赖；1×1 白图只为让 trimesh 走 UV 拆分，管线会重展
        Image.new("RGB", (1, 1), (255, 255, 255)).save(png, format="PNG")
    mtl.write_text(
        "newmtl assetagent_dummy\nKd 1.000 1.000 1.000\nmap_Kd assetagent_white.png\n",
        encoding="utf-8",
    )

    try:
        mesh = trimesh.load(obj, force="mesh", process=False)
    finally:
        obj.unlink(missing_ok=True)
        mtl.unlink(missing_ok=True)

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise MeshError("FBX 里没有可用的网格几何体。")
    save_mesh(mesh, out_dir / (src.stem + ".glb"))
    return out_dir / (src.stem + ".glb")


def _run_ufbx2obj(exe: Path, src: Path, obj: Path) -> None:
    """隔离成函数方便测试 mock：跑内置转换器，失败抛 MeshError。"""
    try:
        result = subprocess.run(  # noqa: S603 - 参数由本模块构造
            [str(exe), str(src), str(obj)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UFBX_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MeshError(f"FBX 解析超时（{UFBX_TIMEOUT}s）") from exc
    except OSError as exc:
        raise MeshError(f"内置转换器无法执行：{exc}") from exc

    if result.returncode != 0 or not obj.exists():
        detail = (result.stderr or result.stdout or "未知原因").strip()[-300:]
        raise MeshError(detail)


def _convert_with_blender(src: Path, out_dir: Path) -> Path:
    target = out_dir / (src.stem + ".glb")
    ok, log = blender.run_script(
        "convert_to_glb.py",
        {"input": str(src), "output": str(target)},
        timeout=BLENDER_TIMEOUT,
    )
    if not ok or not target.exists():
        detail = log.strip() or "未知原因"
        raise MeshError(detail)
    return target


def convert_to_glb(src: Path, out_dir: Path) -> Path:
    """把 src（FBX）转成 out_dir/<stem>.glb，返回 GLB 路径。

    非 FBX 直接原样返回（OBJ/GLB/PLY/STL trimesh 直接读，不该走到这里）。
    两条路都失败时抛 MeshError，消息汇总两侧原因，透给前端。
    """
    src = Path(src)
    out_dir = Path(out_dir)
    if src.suffix.lower() != ".fbx":
        return src
    out_dir.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []

    exe = paths.find_ufbx2obj()
    if exe is not None:
        try:
            return _convert_with_ufbx(exe, src, out_dir)
        except MeshError as exc:
            problems.append(f"内置转换器：{exc}")
    else:
        problems.append("内置转换器缺失（应随应用分发，重新安装可修复）")

    if blender.available():
        try:
            return _convert_with_blender(src, out_dir)
        except MeshError as exc:
            problems.append(f"Blender 回落：{exc}")

    raise MeshError(
        "FBX 转换失败：\n" + "\n".join(problems)
        + "\n请确认文件是有效的二进制 FBX，或先另存为 OBJ / GLB 再导入。"
    )


__all__ = ["convert_to_glb"]

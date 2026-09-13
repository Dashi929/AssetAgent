"""管线步骤 5：烘焙。

从原始高模把 normal / AO / diffuse（底色图集）烘到减面后的低模上。
必须走 Blender（烘焙是 bpy 的活，纯 Python 做不了），所以没有 Blender 时这一步会 skip。

skip 而不是 fail 是刻意的：没有烘焙的资产仍然可以用（只是细节和底色丢了），
而一个失败的任务会让美术丢掉整条链路的结果。

diffuse（方案 A，2026-09-13）：导入模型可能带几十个材质，trimesh 单网格挂不住 ——
在 Blender 里把全部源材质的底色烘到低模新 UV 的一张图集上，管线的其余环节
（视口/缩略图/导出）把这张图集当普通 PBR 底色贴图用即可。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .blender import BlenderUnavailable, available, run_script


def bake_diffuse_software(
    low_mesh: Path,
    high_mesh: Path,
    out_dir: Path,
    resolution: int = 2048,
    *,
    pivot: str = "bottom_center",
    expected_size_m: float | None = None,
) -> dict[str, Any]:
    """无 Blender 的底色转移兜底（方案 A 的纯 Python 实现）。

    原理：低模每个面的中心 → trimesh.proximity 在高模上找最近点 → 该点的
    面所属源贴图 + 重心 UV → 采样出颜色 → 把低模面在**它自己的 UV 三角形**
    里填成这个颜色。得到的 T_basecolor.png 就是低模 UV 上的一张底色图集，
    与 Blender DIFFUSE 烘焙产物同构，后级（挂材质/导出）无感。

    空间对齐是前提：低模在管线里被 normalize_transform 归一化过（缩放到
    expected_size、轴心挪位），高模还是原始尺寸/位置 —— 不把同样的归一化
    套到高模上，最近点投影全打在错误零件上（摩托实测：图集没有一块颜色对）。

    近似性：逐面平色 + 最近点投影（不是射线笼），薄壁/缝隙处可能串色；
    没有 Blender 时的可用解，画质劣于 Blender 路径。
    任何失败都返回 skipped（带原因），绝不抛出 —— 烘焙是可选增强。
    """
    try:
        return _bake_diffuse_software_impl(
            low_mesh,
            high_mesh,
            out_dir,
            resolution,
            pivot=pivot,
            expected_size_m=expected_size_m,
        )
    except Exception as exc:
        return {
            "status": "skipped",
            "method": "software-projection",
            "resolution": resolution,
            "maps": ["diffuse"],
            "files": [],
            "reason": f"软件底色烘焙失败（{type(exc).__name__}: {str(exc)[:120]}）。"
            "提示：pip install \"-e .[mesh]\" 可补 rtree/fast-simplification/xatlas。",
        }


def _bake_diffuse_software_impl(
    low_mesh: Path,
    high_mesh: Path,
    out_dir: Path,
    resolution: int = 2048,
    *,
    pivot: str = "bottom_center",
    expected_size_m: float | None = None,
) -> dict[str, Any]:
    import numpy as np
    import trimesh
    from PIL import Image

    from .gltf_load import load_glb
    from .repair import normalize_transform

    report: dict[str, Any] = {
        "status": "skipped",
        "method": "software-projection",
        "resolution": resolution,
        "maps": ["diffuse"],
        "files": [],
        "reason": None,
    }
    try:
        high, table = load_glb(high_mesh, with_materials=True)
    except Exception as exc:
        report["reason"] = f"高模读取失败（{type(exc).__name__}: {exc}），底色烘焙跳过。"
        return report
    # 与低模同款归一化：管线里的低模已经过 normalize_transform（repair 与
    # decimate 后各一次，最后一次决定最终空间），高模必须进同一空间投影才有意义
    if expected_size_m or pivot != "origin":
        high, _ = normalize_transform(high, pivot=pivot, expected_size_m=expected_size_m)
    textured = sum(1 for t in table.face_texture if t is not None)
    if textured == 0 and not np.any(table.face_factors < 0.999):
        report["reason"] = "源模型没有底色贴图也没有底色因子，无需烘焙。"
        return report

    low = trimesh.load(low_mesh, force="mesh", process=False)
    if low.visual.uv is None or len(low.visual.uv) != len(low.vertices):
        report["reason"] = "低模没有可用 UV，底色烘焙跳过。"
        return report

    # 逐面采样颜色（贴图 × 材质因子；无贴图面用因子）
    centers = np.asarray(low.triangles_center, dtype=np.float64)
    _, _, face_ids = trimesh.proximity.closest_point(high, centers)
    face_ids = np.asarray(face_ids, dtype=np.int64)

    low_faces = np.asarray(low.faces, dtype=np.int64)
    low_uv = np.asarray(low.visual.uv, dtype=np.float64)
    high_triangles = np.asarray(high.triangles, dtype=np.float64)

    face_colors = np.full((len(low_faces), 3), 0.5, dtype=np.float64)
    for index in range(len(low_faces)):
        fid = face_ids[index]
        texture = table.face_texture[fid]
        if texture is not None:
            # 最近点在高模面内的重心坐标 → 该面 UV 空间 → 采样源贴图
            bary = trimesh.triangles.points_to_barycentric(
                high_triangles[[fid]], centers[index][None, :]
            )[0]
            uv = bary @ table.face_uvs[fid]
            width, height = texture.size
            x = min(width - 1, max(0, int(float(uv[0] % 1.0) * width)))
            y = min(height - 1, max(0, int((1.0 - float(uv[1] % 1.0)) * height)))
            color = np.asarray(texture)[y, x] / 255.0 * table.face_factors[fid]
        else:
            color = table.face_factors[fid]
        # 纯黑面照填会糊死接缝；全 0 时留中灰
        face_colors[index] = np.clip(np.where(np.any(color > 0.004), color, 0.5), 0.0, 1.0)

    # 逐像素 Voronoi 填充：每个图集像素取 UV 空间最近的低模面颜色。
    # 不用 PIL 逐面画多边形 —— 细长 UV 三角经常一个像素中心都盖不到（丢色），
    # 且后画的三角会盖掉先画的（摩托的绿色车壳被吃成黑车实测）。
    from scipy.spatial import cKDTree

    centroids_uv = np.mod(low_uv[low_faces].mean(axis=1), 1.0)
    tree = cKDTree(centroids_uv)
    ys, xs = np.mgrid[0:resolution, 0:resolution]
    pixel_uv = np.stack(
        [(xs + 0.5) / resolution, 1.0 - (ys + 0.5) / resolution], axis=-1
    ).reshape(-1, 2)
    _, nearest = tree.query(pixel_uv, k=1, workers=-1)
    atlas_array = face_colors[nearest % len(low_faces)].reshape(
        resolution, resolution, 3
    )
    atlas_array = (np.clip(atlas_array, 0.0, 1.0) * 255.0).astype(np.uint8)

    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "T_basecolor.png"
    Image.fromarray(atlas_array).save(output)
    report["status"] = "ok"
    report["files"] = [str(output)]
    report["reason"] = "无 Blender：normal/AO 跳过，底色用最近点投影软件烘焙（画质略逊）。"
    return report


def bake_textures(
    low_mesh: Path,
    high_mesh: Path | None,
    out_dir: Path,
    resolution: int = 2048,
    *,
    maps: tuple[str, ...] = ("normal", "ao", "diffuse"),
    pivot: str = "bottom_center",
    expected_size_m: float | None = None,
) -> dict[str, Any]:
    """把高模细节烘到低模。返回报告 dict（status = ok / skipped）。

    maps/pivot/expected_size_m 只接受关键字传参 —— 曾经因为位置传参把
    expected_size 塞进 pivot 槽、对齐静默失效（整车颜色串色）。
    pivot/expected_size_m 用于把高模对齐进低模的归一化空间（见
    bake_diffuse_software 与 Blender 脚本内的对齐逻辑）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "status": "skipped",
        "method": "blender-bpy",
        "resolution": resolution,
        "maps": list(maps),
        "files": [],
        "reason": None,
    }

    if not available():
        # 无 Blender：normal/AO 没辙，但底色可以用软件投影兜底 ——
        # "导入进来的模型有颜色"不应当依赖一台装了 Blender 的机器
        soft_report = bake_diffuse_software(
            low_mesh,
            Path(high_mesh),
            out_dir,
            resolution,
            pivot=pivot,
            expected_size_m=expected_size_m,
        )
        if soft_report.get("status") == "ok":
            soft_report["maps"] = ["diffuse"]
            return soft_report
        report["reason"] = (
            "未检测到 Blender，已跳过烘焙。资产仍可导出，但会缺少 normal/AO/底色贴图；"
            "安装 Blender 或在设置里指定其路径后重新跑管线即可补上。"
            + (f"（底色软件烘焙也未成：{soft_report.get('reason')}）" if soft_report.get("reason") else "")
        )
        return report

    if high_mesh is None or not Path(high_mesh).exists():
        report["reason"] = "没有可用于烘焙的高模参考，已跳过烘焙。"
        return report

    try:
        success, log = run_script(
            "bake_textures.py",
            {
                "low": str(low_mesh),
                "high": str(Path(high_mesh)),
                "out": str(out_dir),
                "resolution": str(resolution),
                "maps": ",".join(maps),
                "align_pivot": pivot,
                "align_size": str(expected_size_m or 0),
            },
        )
    except BlenderUnavailable as exc:
        report["reason"] = str(exc)
        return report

    report["log"] = log
    if not success:
        report["reason"] = f"烘焙失败：{log}"
        return report

    # 只认本次 Blender 报告的产物（OK {"files": [...]}）—— out_dir 会跨多次
    # 管线复用，glob 全量会把上一次的旧图混进来（贴图对不上当前 UV）
    produced: list[str] = []
    for line in reversed(log.splitlines()):
        if line.startswith("OK "):
            try:
                produced = list(json.loads(line[3:]).get("files", []))
            except json.JSONDecodeError:
                produced = []
            break
    report["files"] = sorted(produced)
    if not report["files"]:
        report["status"] = "skipped"
        report["reason"] = "Blender 报告成功，但没有产出任何贴图文件。"
        return report
    report["status"] = "ok"
    return report


def attach_baked_material(mesh, texture_files: list[Path]):
    """把烘焙产物挂成低模的 PBR 材质（方案 A 的"贴图回 GLB"一步）。

    - basecolor 是主角：挂上它 `_sanitize_for_export` 的真 PBR 判定才会放行，
      灰色 clay 兜底自动失效，视口/缩略图/导出全链路恢复颜色；
    - normal / AO 一并挂上（trimesh 的 normalTexture / occlusionTexture）；
    - 没有底色图、或网格没有 UV（与顶点数对齐）时原样返回 —— 挂不上就交给
      现有的 clay 兜底，不让烘焙步骤失败。
    """
    import trimesh
    from PIL import Image as PILImage

    by_kind: dict[str, Path] = {}
    for raw in texture_files:
        stem = Path(raw).stem.lower()
        if "basecolor" in stem:
            by_kind["basecolor"] = Path(raw)
        elif "normal" in stem:
            by_kind["normal"] = Path(raw)
        elif "ao" in stem:
            by_kind["ao"] = Path(raw)

    basecolor = by_kind.get("basecolor")
    if basecolor is None or not basecolor.exists():
        return mesh
    uv = getattr(mesh.visual, "uv", None)
    if uv is None or len(uv) != len(mesh.vertices):
        return mesh

    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=PILImage.open(basecolor).convert("RGBA"),
        metallicFactor=0.0,
        roughnessFactor=0.9,
        doubleSided=True,
    )
    normal = by_kind.get("normal")
    if normal is not None and normal.exists():
        material.normalTexture = PILImage.open(normal).convert("RGB")
    ao = by_kind.get("ao")
    if ao is not None and ao.exists():
        material.occlusionTexture = PILImage.open(ao).convert("RGB")

    mesh.visual = trimesh.visual.texture.TextureVisuals(uv=uv, material=material)
    return mesh


__all__ = ["bake_textures"]

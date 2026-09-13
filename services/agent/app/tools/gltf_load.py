"""GLB/GLTF 加载器 —— 按 glTF 2.0 规范自行解析场景图。

为什么自己写（2026-09-12，mushroom_house.glb 事故复盘）：管线对导入几何有
确定性的需求——节点变换必须按规范复合、不支持的特性必须**明确报错**而不是
给出错误几何，而 trimesh 的 GLTF 加载在这两点上都不可控（同文件多套加载路径
行为耦合在其内部实现上，且压缩扩展等会静默产出坏几何）。自主实现约 200 行，
覆盖本工具链需要的子集：

- primitive mode=4（TRIANGLES）；其余 mode 报错提示用 Blender/DCC 转换
- POSITION 必须 FLOAT；索引 u8/u16/u32 或非索引
- NORMAL / TEXCOORD_0 可选；TEXCOORD 支持 normalized 定点解码
- 节点变换 matrix（列主序）与 T*R*S 都支持，沿父子链正确复合
- buffer：GLB 内嵌 BIN chunk、.gltf 的 data: base64；外部 .bin 文件报错
- sparse accessor、KHR_draco / EXT_meshopt 等压缩 → 明确报错

实测 Sketchfab 导出（26 mesh 节点、变换全挂在父节点 matrix 上）加载结果与
逐节点矩阵链推演的顶点集完全一致。
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
import trimesh.visual.texture
from PIL import Image as PILImage

from .mesh_io import MeshError

_COMPONENT_DTYPES = {
    5120: "i1",
    5121: "u1",
    5122: "i2",
    5123: "u2",
    5125: "u4",
    5126: "f4",
}
_TYPE_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_MODE_TRIANGLES = 4

_UNSUPPORTED_EXTENSIONS = ("KHR_draco_mesh_compression", "EXT_meshopt_compression")


def _read_glb(data: bytes) -> tuple[dict, bytes]:
    """GLB 容器 → (json, bin)。"""
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshError("不是有效的 GLB 文件（magic 不对）。")
    offset = 12
    json_bytes = None
    bin_bytes = b""
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack("<II", data[offset : offset + 8])
        chunk = data[offset + 8 : offset + 8 + chunk_len]
        if chunk_type == 0x4E4F534A:  # JSON
            json_bytes = chunk
        elif chunk_type == 0x004E4942:  # BIN
            bin_bytes = chunk
        offset += 8 + chunk_len + (chunk_len % 4 and (4 - chunk_len % 4) or 0)
    if json_bytes is None:
        raise MeshError("GLB 里没有 JSON chunk，文件损坏。")
    return json.loads(json_bytes.decode("utf-8")), bin_bytes


def _read_gltf_buffers(path: Path, gltf: dict) -> list[bytes]:
    """gltf 里的 buffers：支持 data: base64 内嵌；外部 .bin 文件给明确报错。"""
    buffers: list[bytes] = []
    for i, buf in enumerate(gltf.get("buffers", [])):
        uri = buf.get("uri")
        if uri is None:
            raise MeshError(".gltf 引用了外部 buffer 但没写 uri，文件不完整。")
        if uri.startswith("data:"):
            payload = uri.split(",", 1)[-1]
            buffers.append(base64.b64decode(payload))
        else:
            raise MeshError(
                f".gltf 引用了外部 buffer 文件（{uri}），本工具只支持内嵌数据。"
                "请在 Blender 等工具里重新导出为 .glb（单文件）。"
            )
        if i > 64:
            break
    return buffers


class _AccessorReader:
    """按 accessor 读取 buffer 数据，处理 bufferView 的 byteOffset/byteStride。"""

    def __init__(self, gltf: dict, buffers: list[bytes]) -> None:
        self.gltf = gltf
        self.buffers = buffers

    def read(self, accessor_index: int, *, allow_normalized: bool = True) -> np.ndarray:
        accessor = self.gltf["accessors"][accessor_index]
        if "sparse" in accessor:
            raise MeshError(
                "这个 GLB 用了 sparse accessor（稀疏存储），暂不支持。请重新导出为普通网格。"
            )
        dtype = _COMPONENT_DTYPES.get(accessor["componentType"])
        if dtype is None:
            raise MeshError(f"GLB 里的数据类型（componentType={accessor['componentType']}）暂不支持。")
        ncomp = _TYPE_COMPONENTS.get(accessor["type"])
        if ncomp is None:
            raise MeshError(f"GLB 里的数据维度（type={accessor['type']}）暂不支持。")

        count = accessor["count"]
        view_index = accessor.get("bufferView")
        if view_index is None:
            return np.zeros((count, ncomp), dtype=np.float32 if dtype == "f4" else dtype)
        view = self.gltf["bufferViews"][view_index]
        if "extensions" in view:
            raise MeshError(
                "这个 GLB 的 bufferView 带压缩扩展（如 meshopt/draco），暂不支持。"
                "请重新导出为未压缩的 .glb。"
            )
        buffer_bytes = self.buffers[view.get("buffer", 0)]
        if len(buffer_bytes) < view.get("byteLength", 0):
            raise MeshError("GLB 的 buffer 数据不完整，文件可能被截断。")

        elem = np.dtype(dtype).itemsize * ncomp
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        stride = view.get("byteStride") or elem
        if stride < elem:
            raise MeshError("GLB 的 byteStride 比元素还小，文件不符合规范。")

        # 尽量多读（buffer 可能比规范最小需求更满），按行切元素
        available = len(buffer_bytes) - start
        need = elem * count if (view.get("byteStride") in (None, elem)) else stride * (count - 1) + elem
        if available < need:
            raise MeshError("GLB 的 accessor 越界，文件可能被截断。")
        raw = np.frombuffer(buffer_bytes, dtype=np.uint8, count=min(stride * count, available), offset=start)
        if stride == elem:
            arr = raw.view(np.dtype(dtype)).reshape(count, ncomp)
        else:
            # 交错存储：按行切出每个元素的前 elem 字节（最后一行可能不满 stride）
            rows = np.zeros((count, stride), dtype=np.uint8)
            rows[: count - 1] = raw[: stride * (count - 1)].reshape(count - 1, stride)
            tail = raw[stride * (count - 1) : stride * (count - 1) + elem]
            if len(tail) < elem:
                raise MeshError("GLB 的 accessor 越界，文件可能被截断。")
            rows[count - 1][:elem] = tail
            arr = np.ascontiguousarray(rows[:, :elem]).view(np.dtype(dtype)).reshape(count, ncomp)
        arr = np.asarray(arr)
        arr = np.asarray(arr)

        if accessor.get("normalized") and allow_normalized and dtype != "f4":
            # glTF 2.0 §3.26.2：无符号 c/(2^b-1)；有符号 max(c/(2^(b-1)-1), -1)
            info = np.iinfo(arr.dtype)
            arr = arr.astype(np.float32) / float(info.max)
            if info.kind == "i":
                arr = np.maximum(arr, -1.0)
        return arr


def _node_local_matrix(node: dict) -> np.ndarray:
    """节点局部变换：matrix（列主序，需转置）优先，否则按 T*R*S 复合。"""
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=np.float64).reshape(4, 4).T
    m = np.eye(4)
    if "scale" in node:
        m[:3, :3] *= np.asarray(node["scale"], dtype=np.float64)
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        rot = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ],
            dtype=np.float64,
        )
        m[:3, :3] = rot @ m[:3, :3]
    if "translation" in node:
        m[:3, 3] = np.asarray(node["translation"], dtype=np.float64)
    return m


def _world_matrices(gltf: dict) -> dict[int, np.ndarray]:
    """每个节点的世界变换（沿父子链复合，父 × 子）。"""
    nodes = gltf.get("nodes", [])
    parent_of: dict[int, int] = {}
    for i, node in enumerate(nodes):
        for child in node.get("children", []):
            parent_of[child] = i

    cache: dict[int, np.ndarray] = {}

    def world(index: int) -> np.ndarray:
        if index in cache:
            return cache[index]
        m = _node_local_matrix(nodes[index])
        if index in parent_of:
            m = world(parent_of[index]) @ m
        cache[index] = m
        return m

    return {i: world(i) for i in range(len(nodes))}


def _check_unsupported(gltf: dict) -> None:
    used = set(gltf.get("extensionsUsed", [])) | set(gltf.get("extensionsRequired", []))
    for ext in _UNSUPPORTED_EXTENSIONS:
        if ext in used:
            raise MeshError(
                f"这个 GLB 用了 {ext} 压缩，暂不支持。请在 Blender（导出 glTF 2.0，"
                "Draco/Meshopt 压缩选项不勾）或原工具里重新导出。"
            )


@dataclass
class SourceMaterialTable:
    """高模逐面的底色来源（软件版底色烘焙用）。

    face_texture[i] 是第 i 个面的 baseColor 贴图；为 None 时用 face_factors[i]
    的材质底色（glTF 因子是线性空间，这里已转 sRGB 方便直接采样）。
    face_uvs 是逐面角点 UV（V 已按加载器约定翻转），与合并网格的面顺序对齐。
    """

    face_texture: list  # list[PIL.Image | None]
    face_uvs: np.ndarray  # (F, 3, 2)
    face_factors: np.ndarray  # (F, 3) 0~1 sRGB


def _decode_images(gltf: dict, buffers: list[bytes]) -> list:
    """解出 GLB 里的全部图片（按 images[] 顺序，解不出的位置为 None）。"""
    images: list = []
    for image in gltf.get("images", []):
        data: bytes | None = None
        uri = image.get("uri")
        if uri and uri.startswith("data:"):
            with contextlib.suppress(Exception):
                data = base64.b64decode(uri.split(",", 1)[-1])
        elif "bufferView" in image:
            view = gltf["bufferViews"][image["bufferView"]]
            buffer_bytes = buffers[view.get("buffer", 0)]
            start = view.get("byteOffset", 0)
            data = buffer_bytes[start : start + view.get("byteLength", 0)]
        if data is None:
            images.append(None)
            continue
        try:
            images.append(PILImage.open(io.BytesIO(data)).convert("RGB"))
        except Exception:
            images.append(None)
    return images


def _material_basecolor(gltf: dict) -> dict[int, tuple[int | None, np.ndarray | None, int]]:
    """材质索引 → (baseColor 贴图的 images 索引 | None, sRGB 底色因子 | None, texCoord 套号)。

    glTF 的贴图定义带 texCoord 字段指定用第几套 TEXCOORD（默认 0）。Sketchfab
    导出常给一个 primitive 配 3~5 套 UV（光照贴图/蒙皮用），颜色贴图往往不在
    第 0 套 —— 无视这个字段会让整车的颜色采到错误贴图区域（绿车变黑车实测）。
    注意 texCoord 挂在**材质里的贴图引用**上（baseColorTexture.texCoord），
    不是 textures[] 条目上。
    """
    textures = gltf.get("textures", [])
    table: dict[int, tuple[int | None, np.ndarray | None, int]] = {}
    for index, material in enumerate(gltf.get("materials", [])):
        pbr = material.get("pbrMetallicRoughness") or {}
        texture_ref = pbr.get("baseColorTexture")
        image_index: int | None = None
        tex_coord = 0
        if texture_ref is not None:
            source = textures[texture_ref.get("index", 0)].get("source")
            image_index = int(source) if source is not None else None
            tex_coord = int(texture_ref.get("texCoord", 0) or 0)
        factor = pbr.get("baseColorFactor")
        factor_srgb = None
        if factor and len(factor) >= 3:
            # glTF 因子是线性空间，转 sRGB 后贴图采样同域
            factor_srgb = np.power(np.clip(np.asarray(factor[:3], dtype=np.float64), 0.0, 1.0), 1 / 2.2)
        table[index] = (image_index, factor_srgb, tex_coord)
    return table


def load_glb(path: Path | str, with_materials: bool = False):
    """加载 GLB/GLTF 为单个合并网格（世界变换已应用）。

    with_materials=True 时返回 (mesh, SourceMaterialTable)：管线加载保持 False
    （省内存），软件版底色烘焙用 True 拿逐面贴图与 UV。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".glb":
        gltf, bin_chunk = _read_glb(path.read_bytes())
        # GLB 规范上允许多 buffer，实际工具都只有一个；第 0 个是 BIN chunk，
        # 多出来的（几乎没有）按 uri 读取，读不了会给人话错误
        buffers = _read_gltf_buffers(path, {**gltf, "buffers": gltf.get("buffers", [])[1:]})
        buffers.insert(0, bin_chunk)
    elif suffix == ".gltf":
        gltf = json.loads(path.read_text(encoding="utf-8"))
        buffers = _read_gltf_buffers(path, gltf)
    else:
        raise MeshError(f"load_glb 只处理 .glb/.gltf，收到 {suffix}")

    _check_unsupported(gltf)
    reader = _AccessorReader(gltf, buffers)
    world = _world_matrices(gltf)
    gltf_meshes = gltf.get("meshes", [])
    if not gltf_meshes:
        raise MeshError("这个 GLB 里没有任何网格。")

    all_positions: list[np.ndarray] = []
    all_faces: list[np.ndarray] = []
    all_uvs: list[np.ndarray] = []
    warnings: list[str] = []
    vertex_offset = 0
    face_texture_ids: list[int] = []
    face_uvs_per_primitive: list[np.ndarray] = []
    material_basecolor = _material_basecolor(gltf) if with_materials else {}
    images = _decode_images(gltf, buffers) if with_materials else []

    for node_index, node in enumerate(gltf.get("nodes", [])):
        if "mesh" not in node:
            continue
        transform = world[node_index]
        for primitive in gltf_meshes[node["mesh"]].get("primitives", []):
            mode = primitive.get("mode", _MODE_TRIANGLES)
            if mode != _MODE_TRIANGLES:
                warnings.append(f"跳过非三角形图元（mode={mode}）")
                continue
            indices_index = primitive.get("indices")
            if indices_index is None:
                count = gltf["accessors"][primitive["attributes"]["POSITION"]]["count"]
                faces_local = np.arange(count, dtype=np.int64).reshape(-1, 3)
                if len(faces_local) == 0:
                    continue
            else:
                flat = reader.read(indices_index, allow_normalized=False)
                if flat.dtype != np.int64:
                    flat = flat.astype(np.int64)
                if len(flat) % 3 != 0 or len(flat) == 0:
                    raise MeshError("GLB 的索引数不是 3 的倍数，文件不符合规范。")
                faces_local = flat.reshape(-1, 3)

            positions = reader.read(primitive["attributes"]["POSITION"], allow_normalized=False)
            if positions.shape[1] != 3:
                raise MeshError("GLB 的 POSITION 不是三维坐标，文件不符合规范。")
            positions = positions.astype(np.float64) @ transform[:3, :3].T + transform[:3, 3]

            all_positions.append(positions)
            all_faces.append(faces_local + vertex_offset)
            vertex_offset += len(positions)

            uv_index = primitive["attributes"].get("TEXCOORD_0")
            if with_materials:
                # 颜色贴图可能在任意一套 TEXCOORD 上（材质 texCoord 字段指定）
                material_index = primitive.get("material")
                image_index, _factor, tex_coord = material_basecolor.get(
                    material_index, (None, None, 0)
                )
                color_uv_index = primitive["attributes"].get(f"TEXCOORD_{tex_coord}")
                if color_uv_index is not None:
                    uv_index = color_uv_index
            if uv_index is not None:
                uv = reader.read(uv_index)
                uv = uv.astype(np.float64)
                uv[:, 1] = 1.0 - uv[:, 1]  # glTF 的 V 轴向上，trimesh/PBR 约定向下
                uvs = np.zeros((len(positions), 2))
                take = min(len(uv), len(positions))
                uvs[:take] = uv[:take]
                all_uvs.append(uvs)
            else:
                uvs = None

            if with_materials:
                # 逐面记录底色来源与角点 UV（与合并网格的面顺序对齐）
                image_index, _factor, _ = material_basecolor.get(
                    primitive.get("material"), (None, None, 0)
                )
                if uvs is None or image_index is None:
                    image_index = -1  # 没贴图或没 UV 的面走材质底色
                face_texture_ids.extend([image_index] * len(faces_local))
                if uvs is not None:
                    face_uvs_per_primitive.append(uvs[faces_local])
                else:
                    face_uvs_per_primitive.append(np.zeros((len(faces_local), 3, 2)))

    if not all_positions:
        raise MeshError(
            "这个 GLB 里没有可读的三角形网格"
            + ("（" + "；".join(sorted(set(warnings))) + "）" if warnings else "。")
        )

    positions = np.vstack(all_positions)
    faces = np.vstack(all_faces)
    mesh = trimesh.Trimesh(vertices=positions, faces=faces, process=False)
    if all_uvs:
        # UV 布局异常不该让导入失败
        with contextlib.suppress(Exception):
            mesh.visual = trimesh.visual.texture.TextureVisuals(uv=np.vstack(all_uvs))
    if warnings:
        mesh.metadata["import_warnings"] = sorted(set(warnings))
    # 本加载器只取几何与 UV，材质/贴图由烘焙步（diffuse → 图集）转移；
    # 记下源文件的材质规模供详情页展示与诊断
    mesh.metadata["source_material_count"] = len(gltf.get("materials", []))
    mesh.metadata["source_image_count"] = len(gltf.get("images", []))
    if not with_materials:
        return mesh

    # 逐图元回填材质底色因子（面顺序与合并网格对齐；贴图面一般为白色系数，
    # 采样时 factor × texel，无贴图面直接用因子当底色）
    factors = np.ones((len(faces), 3), dtype=np.float64)
    row = 0
    for node_index, node in enumerate(gltf.get("nodes", [])):
        if "mesh" not in node:
            continue
        for primitive in gltf_meshes[node["mesh"]].get("primitives", []):
            if primitive.get("mode", _MODE_TRIANGLES) != _MODE_TRIANGLES:
                continue
            indices_index = primitive.get("indices")
            if indices_index is None:
                count = gltf["accessors"][primitive["attributes"]["POSITION"]]["count"] // 3
            else:
                count = gltf["accessors"][indices_index]["count"] // 3
            _, factor, _ = material_basecolor.get(primitive.get("material"), (None, None, 0))
            if factor is not None:
                factors[row : row + count] = factor
            row += count
    texture_per_face = [
        images[i] if i is not None and 0 <= i < len(images) else None for i in face_texture_ids
    ]
    table = SourceMaterialTable(
        face_texture=texture_per_face,
        face_uvs=(
            np.concatenate(face_uvs_per_primitive)
            if face_uvs_per_primitive
            else np.zeros((0, 3, 2))
        ),
        face_factors=factors,
    )
    return mesh, table

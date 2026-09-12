"""GLB/GLTF 自有加载器测试。

手工构造 GLB 字节流，覆盖真实事故（mushroom_house.glb）里的关键形态：
- 变换写在父节点 matrix（列主序）、mesh 节点为恒等子节点 —— 事故文件的结构
- 交错存储（bufferView.byteStride）
- 非索引图元、u16 索引
- 外部 .bin / 压缩扩展 → 明确报错而不是错误几何
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from app.tools.gltf_load import load_glb
from app.tools.mesh_io import MeshError


def _accessor_json(component_type: int, type_name: str, count: int, view: int, byte_offset: int = 0, **kw) -> dict:
    return {"componentType": component_type, "type": type_name, "count": count,
            "bufferView": view, "byteOffset": byte_offset, **kw}


def _build_glb(gltf: dict, bin_data: bytes) -> bytes:
    json_bytes = json.dumps(gltf).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    bin_data += b"\0" * ((4 - len(bin_data) % 4) % 4)
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(json_bytes) + 8 + len(bin_data))
    out += struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    out += struct.pack("<II", len(bin_data), 0x004E4942) + bin_data
    return out


def _mat(col_major_12: list[float]) -> list[float]:
    """给 12 个列主序元素补满 4x4（第 4 行 0,0,0,1）。"""
    return col_major_12 + [0.0, 0.0, 0.0, 1.0]


RX90 = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, -1.0, 0.0, 0.0]  # 绕 X +90°：Y→Z


def _simple_mesh_gltf(positions: np.ndarray, indices: np.ndarray | None = None,
                      component_type: int = 5125) -> tuple[dict, bytes]:
    """单 mesh 单节点（恒等变换）最小 GLB。"""
    pos_bytes = positions.astype("<f4").tobytes()
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes)}]
    attributes = {"POSITION": 0}
    chunks = [pos_bytes]
    gltf: dict = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": 0}],
        "bufferViews": views,
        "accessors": [_accessor_json(5126, "VEC3", len(positions), 0)],
        "meshes": [{"primitives": [{"attributes": attributes, "mode": 4}]}],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    if indices is not None:
        idx_bytes = indices.astype("<u2" if component_type == 5123 else "<u4").tobytes()
        offset = len(pos_bytes)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(idx_bytes)})
        gltf["accessors"].append(_accessor_json(component_type, "SCALAR", len(indices), 1))
        gltf["meshes"][0]["primitives"][0]["indices"] = 1
        chunks.append(idx_bytes)
    bin_data = b"".join(chunks)
    gltf["buffers"][0]["byteLength"] = len(bin_data)
    return gltf, bin_data


def test_matrix_parent_chain_is_applied(tmp_path):
    """事故文件形态：变换挂在父节点 matrix，mesh 节点是恒等子节点。"""
    # 三角形顶点：(0,0,0) (1,0,0) (0,1,0)；父节点 matrix = Rx90 + 平移 ty=5
    # Rx90: (x,y,z) → (x,-z,y)，再加 ty=5 → (1,0,0)→(1,5,0), (0,1,0)→(0,5,1)
    positions = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    gltf, bin_data = _simple_mesh_gltf(positions, np.array([0, 1, 2], dtype=np.uint32))
    parent_matrix = _mat(RX90)
    parent_matrix[13] = 5.0  # 列主序：index 13 = translation Y
    gltf["nodes"] = [
        {"matrix": parent_matrix, "children": [1]},
        {"mesh": 0},
    ]
    gltf["scenes"] = [{"nodes": [0]}]

    path = tmp_path / "matrix_chain.glb"
    path.write_bytes(_build_glb(gltf, bin_data))
    mesh = load_glb(path)

    assert len(mesh.faces) == 1
    expected = np.array([[0.0, 5.0, 0.0], [1.0, 5.0, 0.0], [0.0, 5.0, 1.0]])
    assert np.allclose(np.sort(mesh.vertices, axis=0), np.sort(expected, axis=0), atol=1e-6)


def test_interleaved_byte_stride(tmp_path):
    """POSITION/NORMAL 交错存储（显式 byteStride）要读对列。"""
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype="<f4")
    normals = np.array([[0, 0, 1], [0, 0, 1], [0, 0, 1]], dtype="<f4")
    interleaved = np.empty(3, dtype=[("pos", "<f4", 3), ("nrm", "<f4", 3)])
    interleaved["pos"] = positions
    interleaved["nrm"] = normals
    raw = interleaved.tobytes()

    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(raw)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(raw), "byteStride": 24}],
        "accessors": [
            _accessor_json(5126, "VEC3", 3, 0, 0),
            _accessor_json(5126, "VEC3", 3, 0, 12),
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "mode": 4}]}],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    path = tmp_path / "interleaved.glb"
    path.write_bytes(_build_glb(gltf, raw))
    mesh = load_glb(path)
    assert np.allclose(np.sort(mesh.vertices, axis=0), np.sort(positions.astype(np.float64), axis=0))


def test_non_indexed_primitive(tmp_path):
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=np.float32)
    gltf, bin_data = _simple_mesh_gltf(positions, None)
    path = tmp_path / "nonindexed.glb"
    path.write_bytes(_build_glb(gltf, bin_data))
    mesh = load_glb(path)
    assert len(mesh.faces) == 2


def test_uint16_indices(tmp_path):
    positions = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    gltf, bin_data = _simple_mesh_gltf(positions, np.array([0, 1, 2], dtype=np.uint16), component_type=5123)
    path = tmp_path / "u16.glb"
    path.write_bytes(_build_glb(gltf, bin_data))
    mesh = load_glb(path)
    assert len(mesh.faces) == 1


def test_external_bin_gives_clear_error(tmp_path):
    gltf, _ = _simple_mesh_gltf(np.zeros((3, 3), dtype=np.float32), None)
    gltf["buffers"][0]["uri"] = "scene.bin"
    path = tmp_path / "external.gltf"
    path.write_text(json.dumps(gltf), encoding="utf-8")
    with pytest.raises(MeshError, match="重新导出为 .glb"):
        load_glb(path)


def test_draco_extension_gives_clear_error(tmp_path):
    positions = np.zeros((3, 3), dtype=np.float32)
    gltf, bin_data = _simple_mesh_gltf(positions, None)
    gltf["extensionsUsed"] = ["KHR_draco_mesh_compression"]
    path = tmp_path / "draco.glb"
    path.write_bytes(_build_glb(gltf, bin_data))
    with pytest.raises(MeshError, match="压缩"):
        load_glb(path)


def test_real_world_mushroom_bounds(tmp_path):
    """真实事故文件回归（文件在则验，不在则跳过）。"""
    source = Path(r"C:\Users\11020\Downloads\mushroom_house.glb")
    if not source.exists():
        pytest.skip("本机没有 mushroom_house.glb")
    mesh = load_glb(source)
    # 两级反向 ±90° 根旋转相抵消 → 世界坐标 ≈ 各部件局部坐标的并
    assert len(mesh.faces) == 160486
    lo, hi = mesh.bounds
    assert hi[1] > lo[1]  # Y 跨度合理（帽在 +Y 15m 一带）
    assert lo[1] == pytest.approx(-2.0, abs=0.01)  # 声明的全局 min Y
    uv = mesh.visual.uv
    assert uv is not None and len(uv) == len(mesh.vertices)

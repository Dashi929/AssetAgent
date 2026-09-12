"""测试夹具。

每个测试用独立的数据目录，避免互相污染，也避免碰到开发机上真实的资产库。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让 `import app` 在未执行 pip install -e . 的情况下也能工作
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSETAGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASSETAGENT_ALLOW_MOCK_FALLBACK", "true")
    monkeypatch.setenv("ASSETAGENT_ROUTE_MODE", "byok")
    # 清掉可能从 .env 里读进来的真实 Key，测试不允许联网
    for key in ("MESHY_API_KEY", "TRIPO_API_KEY", "RODIN_API_KEY", "HUNYUAN3D_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    from app.config import reset_settings_cache
    from app.providers import registry

    reset_settings_cache()
    registry.reload()
    yield
    reset_settings_cache()


@pytest.fixture
def client():
    """走真实 HTTP 语义的 TestClient。

    with 语句会触发 lifespan（数据目录初始化、registry 装载）。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_obj(tmp_path):
    """一个"干净"的样例资产：轴心在底面中心（Y-up：底面贴 Y=0，XZ 居中）、命名合规。"""
    import trimesh

    mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.5)
    mesh.vertices[:, 0] -= (mesh.bounds[0][0] + mesh.bounds[1][0]) / 2
    mesh.vertices[:, 2] -= (mesh.bounds[0][2] + mesh.bounds[1][2]) / 2
    mesh.vertices[:, 1] -= mesh.vertices[:, 1].min()  # 底面贴 Y=0
    path = tmp_path / "SM_Test_Prop.obj"
    path.write_text(mesh.export(file_type="obj"), encoding="utf-8")
    return path


@pytest.fixture
def dirty_glb(tmp_path):
    """一个"脏"资产：高面数、游离组件、退化面、无 UV —— 模拟云端生成模型的典型状态。"""
    import numpy as np
    import trimesh

    base = trimesh.creation.icosphere(subdivisions=4, radius=0.5)
    stray = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    stray.apply_translation([0.9, 0.0, 0.03])
    merged = trimesh.util.concatenate([base, stray])
    faces = np.vstack([merged.faces, np.array([[0, 0, 1]], dtype=np.int64)])
    mesh = trimesh.Trimesh(vertices=merged.vertices, faces=faces, process=False)
    path = tmp_path / "dirty.glb"
    path.write_bytes(mesh.export(file_type="glb"))
    return path


@pytest.fixture
def with_llm_key():
    """给测试进程塞一个 LLM Key（真实 settings 机制），结束清除。"""
    from app.config import get_settings

    settings = get_settings()
    settings.set_provider_key("llm", "test-key-12345")
    yield
    settings.set_provider_key("llm", "")

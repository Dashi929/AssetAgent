"""混元3D Provider 与 FBX 导入的测试。

2026-09-11 拍板跟进：Meshy / Tripo / 混元3D 三家自由可选（产品策划文档 12）；
工作流 C 的导入要支持 fbx / obj。这里把两条新链路的行为钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import paths
from app.providers import registry
from app.providers.base import ProviderError
from app.providers.hunyuan3d import Hunyuan3DProvider, split_credentials
from app.providers.registry import PRIORITY as PROVIDER_PRIORITY
from app.tools import blender
from app.tools.convert import convert_to_glb
from app.tools.mesh_io import MeshError, load_mesh

# ------------------------------------------------------------------ 混元3D


def test_hunyuan3d_registered_and_priority():
    """拍板的三家（meshy/tripo/hunyuan3d）都在注册表里，rodin 保留在末位。"""
    names = {p.name for p in registry._build().values()}
    assert {"meshy", "tripo", "hunyuan3d", "rodin"} <= names
    assert PROVIDER_PRIORITY == ("meshy", "tripo", "hunyuan3d", "rodin")


def test_hunyuan3d_appears_in_settings_snapshot(client):
    snapshot = client.get("/api/settings").json()
    names = {p["name"] for p in snapshot["providers"]}
    assert "hunyuan3d" in names


def test_split_credentials():
    assert split_credentials("AKIDexample:secret123") == ("AKIDexample", "secret123")
    # SecretKey 里允许出现冒号：只在第一个冒号处切
    assert split_credentials("id:ke:y") == ("id", "ke:y")
    with pytest.raises(ProviderError):
        split_credentials("只有一个值")


def test_tc3_headers_shape(monkeypatch):
    """TC3 签名头结构固定：算法前缀、Credential 起始、SignedHeaders 三件套。"""
    provider = Hunyuan3DProvider(api_key="AKIDexample:secret", base_url="https://ai3d.tencentcloudapi.com")
    headers = provider._tc3_headers("QueryHunyuanTo3DRapidJob", "{}")
    assert headers["X-TC-Action"] == "QueryHunyuanTo3DRapidJob"
    assert headers["Host"] == "ai3d.tencentcloudapi.com"
    assert headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDexample/")
    assert "SignedHeaders=content-type;host;x-tc-action" in headers["Authorization"]


def test_hunyuan3d_estimate_cost_uses_unit_price():
    provider = Hunyuan3DProvider(api_key="id:key")
    # 混元按量计费，沿用"次数 × 单价"的默认估算
    assert provider.estimate_cost(3, 2.5) == 7.5


# ------------------------------------------------------------------ FBX 导入


def _fbx_bytes() -> bytes:
    # 内容不重要：走到转换这一步之前只看扩展名
    return b"Kaydara FBX Binary  \x00\x1a\x00fake"


def test_import_mesh_rejects_unknown_suffix(client):
    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("model.max", b"junk", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert ".max" in response.json()["detail"]


def test_import_fbx_without_blender_returns_400_and_archives(client, tmp_path, monkeypatch):
    """FBX 依赖 Blender 转换：没有 Blender 时给 400 人话，且资产不留在处理中状态。"""
    monkeypatch.setattr(blender, "available", lambda: False)

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Thing.fbx", _fbx_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "Blender" in response.json()["detail"]

    # 资产已被归档（列表接口默认不含归档），而不是以 PROCESSING 挂着误导用户
    assert client.get("/api/assets").json() == []


def test_import_fbx_conversion_failure_surfaces_log(client, monkeypatch):
    """Blender 在但转换失败：报错要带上 Blender 的日志尾部，方便排障。"""
    monkeypatch.setattr(blender, "available", lambda: True)
    monkeypatch.setattr(blender, "run_script", lambda *a, **k: (False, "ERR ImportError: bad fbx"))

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Thing.fbx", _fbx_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "bad fbx" in response.json()["detail"]


def test_import_fbx_success_uses_glb_working_copy(client, monkeypatch):
    """转换成功：版本节点的 mesh_path 指向 GLB 工作副本，原始 FBX 留在 source/。"""
    captured: dict = {}

    def fake_run_script(script_name, args, timeout=300):
        captured["args"] = args
        out = Path(args["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"glTF-fake")
        return True, "OK {}"

    monkeypatch.setattr(blender, "available", lambda: True)
    monkeypatch.setattr(blender, "run_script", fake_run_script)

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Thing.fbx", _fbx_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 201
    asset_id = response.json()["asset"]["id"]
    versions = client.get(f"/api/assets/{asset_id}").json()["versions"]
    assert versions, "导入后必须有 import 版本节点"

    node = versions[-1]
    assert node["mesh_path"].endswith(".glb")
    assert node["params"].get("converted_from", "").endswith(".fbx")
    assert Path(node["params"]["converted_from"]).is_file()
    assert captured["args"]["input"].endswith(".fbx")


def test_convert_to_glb_passes_paths_to_blender(tmp_path, monkeypatch):
    """convert_to_glb 只做拼装：脚本名、输入、输出都交给 blender.run_script。"""
    src = tmp_path / "SM_Any.fbx"
    src.write_bytes(b"fbx")

    seen: dict = {}

    def fake_run_script(script_name, args, timeout=300):
        seen["script"] = script_name
        seen["args"] = args
        out = Path(args["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"glb")
        return True, "OK {}"

    monkeypatch.setattr(blender, "available", lambda: True)
    monkeypatch.setattr(blender, "run_script", fake_run_script)

    result = convert_to_glb(src, tmp_path / "out")
    assert seen["script"] == "convert_to_glb.py"
    assert seen["args"] == {"input": str(src), "output": str(tmp_path / "out" / "SM_Any.glb")}
    assert result == tmp_path / "out" / "SM_Any.glb"


def test_load_mesh_rejects_fbx_with_guidance(tmp_path):
    """绕过导入入口直接把 FBX 喂给 load_mesh：给"去导入入口"的指引而不是格式列表。"""
    fbx = tmp_path / "x.fbx"
    fbx.write_bytes(b"fbx")
    with pytest.raises(MeshError) as excinfo:
        load_mesh(fbx)
    assert "导入" in str(excinfo.value)


def test_dev_recipes_still_found():
    """convert 脚本依赖 recipes 定位；确认本轮改动没破坏开发模式解析。"""
    assert (paths.find_recipes_dir() / "bpy" / "convert_to_glb.py").is_file()

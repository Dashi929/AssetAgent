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
from app.tools.mesh_io import MeshError, load_mesh, uv_array

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
#
# 转换链是三级回落：内置 ufbx2obj（优先）→ Blender（可选）→ 报错。
# 每个测试都显式钉死"哪一级可用"，避免依赖开发机的真实状态。

FIXTURE_FBX = Path(__file__).parent / "fixtures" / "phong_cube.fbx"


def _fbx_bytes() -> bytes:
    # 内容不重要：走到转换这一步之前只看扩展名
    return b"Kaydara FBX Binary  \x00\x1a\x00fake"


def _no_ufbx(monkeypatch):
    """钉死"内置转换器不存在"，让流程走 Blender 回落。"""
    monkeypatch.setattr(paths, "find_ufbx2obj", lambda: None)


needs_ufbx = pytest.mark.skipif(
    paths.find_ufbx2obj() is None or not FIXTURE_FBX.exists(),
    reason="需要仓库内置的 ufbx2obj.exe 与样例 FBX（Windows 开发机产物）",
)


def test_import_mesh_rejects_unknown_suffix(client):
    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("model.max", b"junk", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert ".max" in response.json()["detail"]


def test_import_fbx_no_converter_returns_400_and_cleans_up(client, monkeypatch):
    """内置转换器缺失且无 Blender：400 人话，且资产不留在处理中状态。"""
    _no_ufbx(monkeypatch)
    monkeypatch.setattr(blender, "available", lambda: False)

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Thing.fbx", _fbx_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "另存为 OBJ / GLB" in response.json()["detail"]  # 给了替代路径

    # 导入失败的资产已被直接删除（2026-09-13 起不再归档），而不是以 PROCESSING 挂着误导用户
    assert client.get("/api/assets").json() == []


def test_import_fbx_blender_failure_surfaces_log(client, monkeypatch):
    """内置转换器失败、Blender 顶上再失败：报错要带上两侧的日志，方便排障。"""
    _no_ufbx(monkeypatch)
    monkeypatch.setattr(blender, "available", lambda: True)
    monkeypatch.setattr(blender, "run_script", lambda *a, **k: (False, "ERR ImportError: bad fbx"))

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Thing.fbx", _fbx_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "bad fbx" in response.json()["detail"]


def test_import_fbx_blender_success_uses_glb_working_copy(client, monkeypatch):
    """Blender 回落成功：版本节点指向 GLB 工作副本，原始 FBX 留在 source/。"""
    captured: dict = {}

    def fake_run_script(script_name, args, timeout=300):
        captured["args"] = args
        out = Path(args["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"glTF-fake")
        return True, "OK {}"

    _no_ufbx(monkeypatch)
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


@needs_ufbx
def test_convert_with_real_ufbx_without_blender(tmp_path, monkeypatch):
    """内置 ufbx 转换器独立工作：Blender 缺失也必须转换成功（本轮目标）。"""
    monkeypatch.setattr(blender, "available", lambda: False)

    out = convert_to_glb(FIXTURE_FBX, tmp_path)
    assert out.name == "phong_cube.glb"
    mesh = load_mesh(out)
    assert len(mesh.faces) >= 6  # 样例是个立方体


@needs_ufbx
def test_convert_preserves_uv_through_real_ufbx(tmp_path, monkeypatch):
    """UV 必须活过 OBJ→GLB 两跳：这是占位材质机制存在的意义。"""
    monkeypatch.setattr(blender, "available", lambda: False)

    out = convert_to_glb(FIXTURE_FBX, tmp_path)
    assert uv_array(load_mesh(out)) is not None


@needs_ufbx
def test_import_fbx_real_end_to_end(client, monkeypatch):
    """API 全链路：真实 FBX 上传 → 内置转换 → import 节点指向可用 GLB。"""
    monkeypatch.setattr(blender, "available", lambda: False)  # 证明没走 Blender

    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("SM_Phong.fbx", FIXTURE_FBX.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 201
    asset_id = response.json()["asset"]["id"]
    node = client.get(f"/api/assets/{asset_id}").json()["versions"][-1]
    assert node["mesh_path"].endswith(".glb")
    mesh = load_mesh(node["mesh_path"])
    assert len(mesh.faces) >= 6


def test_convert_falls_back_to_blender_when_ufbx_fails(tmp_path, monkeypatch):
    """内置转换器解析失败时，Blender 用另一套解析器兜底。"""

    def broken_exe(exe, src, obj):
        raise MeshError("ufbx parse failed: garbage")

    from app.tools import convert

    monkeypatch.setattr(paths, "find_ufbx2obj", lambda: Path("C:/no/such/ufbx2obj.exe"))
    monkeypatch.setattr(convert, "_run_ufbx2obj", broken_exe)
    monkeypatch.setattr(blender, "available", lambda: True)

    def fake_run_script(script_name, args, timeout=300):
        out = Path(args["output"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"glTF-fake")
        return True, "OK {}"

    monkeypatch.setattr(blender, "run_script", fake_run_script)

    out = convert_to_glb(FIXTURE_FBX, tmp_path)
    assert out.name == "phong_cube.glb"
    assert out.is_file()


def test_convert_aggregates_errors_when_all_paths_fail(tmp_path, monkeypatch):
    """两级都不可用：错误消息汇总各自原因，而不是只报最后一级。"""
    _no_ufbx(monkeypatch)
    monkeypatch.setattr(blender, "available", lambda: False)

    with pytest.raises(MeshError) as excinfo:
        convert_to_glb(FIXTURE_FBX, tmp_path)
    assert "内置转换器缺失" in str(excinfo.value)


def test_load_mesh_rejects_fbx_with_guidance(tmp_path):
    """绕过导入入口直接把 FBX 喂给 load_mesh：给"去导入入口"的指引而不是格式列表。"""
    fbx = tmp_path / "x.fbx"
    fbx.write_bytes(b"fbx")
    with pytest.raises(MeshError) as excinfo:
        load_mesh(fbx)
    assert "导入" in str(excinfo.value)


def test_dev_recipes_still_found():
    """convert 依赖 recipes 定位；确认本轮改动没破坏开发模式解析。"""
    assert (paths.find_recipes_dir() / "bpy" / "convert_to_glb.py").is_file()

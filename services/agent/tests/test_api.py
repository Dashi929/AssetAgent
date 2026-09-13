"""HTTP 接口层测试。

为什么必须有这一层：单元测试都是**直接调函数**，`routers/` 里的逻辑一行都没被跑过。
而 router 恰恰是问题最容易藏身的地方 —— 参数校验、状态推进顺序、错误码、路径安全，
这些都不在 store / tools 的测试范围内。

这里用 FastAPI TestClient 走真实 HTTP 语义，覆盖：
1. 完整工作流 A：上传 → 生成 → 轮询 → 挑选 → 管线 → 校验 → 导出
2. 工作流 C：导入已有网格 → 直接跑管线
3. 安全与边界：文件接口越权、校验未过时导出被拦截、未知资产 404
4. BYOK：写入 / 掩码回显 / 清除（Key 绝不能明文回显）
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image


def make_png_bytes(size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (200, 198, 190)).save(buffer, format="PNG")
    return buffer.getvalue()


def wait_for_job(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    """轮询任务直到结束。任务在 sidecar 侧是落盘的，所以这里就是前端的真实行为。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"任务超时未结束：{last}")


# ------------------------------------------------------------------ 基础接口


def test_health_and_meta(client):
    assert client.get("/api/health").json()["status"] == "ok"

    presets = client.get("/api/presets").json()
    assert "prop_default" in presets["spec_presets"]
    assert "unity" in presets["export_presets"]

    diagnostics = client.get("/api/diagnostics").json()
    assert "data_dir" in diagnostics
    assert diagnostics["mesh_backends"]["decimate"] in {
        "fast-simplification",
        "pymeshlab",
        "未安装",
    }


def test_settings_snapshot_never_leaks_key(client):
    """设置接口只能回掩码 —— 明文 Key 绝不出现在任何响应里。"""
    secret = "sk-super-secret-value-12345"
    response = client.put("/api/settings/byok", json={"provider": "meshy", "key": secret})
    assert response.status_code == 200, response.text
    assert secret not in response.text

    snapshot = client.get("/api/settings").json()
    meshy = next(p for p in snapshot["providers"] if p["name"] == "meshy")
    assert meshy["has_key"] is True
    assert meshy["key_masked"].endswith(secret[-4:])
    assert secret not in client.get("/api/settings").text

    cleared = client.delete("/api/settings/byok/meshy").json()
    meshy = next(p for p in cleared["providers"] if p["name"] == "meshy")
    assert meshy["has_key"] is False


def test_unknown_asset_returns_404(client):
    assert client.get("/api/assets/asset_does_not_exist").status_code == 404
    assert client.get("/api/jobs/job_does_not_exist").status_code == 404


# ------------------------------------------------------------------ 工作流 A


def test_workflow_a_full_http_roundtrip(client):
    """概念图 → 生成 → 挑选 → 管线 → 校验 → 导出，全程走 HTTP。"""
    # 1) 上传概念图
    upload = client.post(
        "/api/assets/upload",
        files=[("files", ("concept.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_Http_Prop", "preset_key": "prop_default", "prompt": ""},
    )
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["asset"]["id"]
    assert upload.json()["asset"]["status"] == "draft"

    # 2) 生成（无 Key 时自动落到 mock provider）
    started = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 2})
    assert started.status_code == 202, started.text
    payload = started.json()
    assert payload["provider"] == "mock"
    assert payload["estimate_cny"] == 0.0

    job = wait_for_job(client, payload["job"]["id"])
    assert job["status"] == "succeeded", job

    # 3) 资产进入"待挑选"，并且变体都落盘了
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["status"] == "awaiting_pick"
    assert len(detail["variants"]) == 2
    assert all(v["face_count"] for v in detail["variants"])
    # 每个变体都同时是一个版本节点（变体对比与版本回滚共用一套机制）
    assert sum(1 for v in detail["versions"] if v["op"] == "generate") == 2

    variant_id = detail["variants"][0]["id"]

    # 4) 挑选变体
    picked = client.post(f"/api/assets/{asset_id}/pick", json={"variant_id": variant_id})
    assert picked.status_code == 200, picked.text
    assert picked.json()["asset"]["picked_variant_id"] == variant_id

    # 5) 跑管线
    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    pipeline_job = wait_for_job(client, pipeline.json()["job"]["id"])
    assert pipeline_job["status"] == "succeeded", pipeline_job

    detail = client.get(f"/api/assets/{asset_id}").json()
    ops = [v["op"] for v in detail["versions"]]
    for expected in ("repair", "decimate", "uv", "bake"):
        assert expected in ops, f"版本树缺少 {expected} 节点：{ops}"

    # 6) 校验报告
    assert detail["reports"], "没有产出校验报告"
    report = detail["reports"][-1]
    assert len(report["results"]) >= 7
    assert report["stats"]["faces"] > 0
    assert detail["asset"]["status"] in {"validated", "awaiting_validation"}

    # 7) 导出
    exported = client.post(
        f"/api/assets/{asset_id}/export", json={"preset": "unity", "allow_failed_export": True}
    )
    assert exported.status_code == 200, exported.text
    files = exported.json()["export"]["files"]
    assert any(f.endswith(".glb") for f in files)
    assert any(f.endswith("manifest.json") for f in files)

    # 8) 产物能通过文件接口读回来
    glb = next(f for f in files if f.endswith(".glb"))
    served = client.get("/api/files", params={"path": glb})
    assert served.status_code == 200
    assert served.content[:4] == b"glTF", "读回来的不是 GLB"

    assert client.get(f"/api/assets/{asset_id}").json()["asset"]["status"] == "exported"


def test_upload_rejects_non_image(client):
    response = client.post(
        "/api/assets/upload",
        files=[("files", ("model.glb", b"not an image", "application/octet-stream"))],
        data={"name": "SM_Bad", "preset_key": "prop_default"},
    )
    assert response.status_code == 400
    assert "不支持的文件类型" in response.json()["detail"]


def test_generate_without_reference_is_rejected(client):
    """没有概念图也没有文字描述时，生成必须给出人话错误，而不是让 Provider 去炸。"""
    created = client.post("/api/assets", json={"name": "SM_Empty", "source": "image"}).json()
    asset_id = created["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 1})
    assert response.status_code == 400
    assert "无法生成" in response.json()["detail"]


def test_unknown_preset_is_rejected(client):
    response = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_X", "preset_key": "no_such_preset"},
    )
    assert response.status_code == 400
    assert "未知的规格预设" in response.json()["detail"]


# ------------------------------------------------------------------ 工作流 C


def test_workflow_c_import_mesh_then_pipeline(client, dirty_glb):
    imported = client.post(
        "/api/assets/import-mesh",
        files=[("file", ("dirty.glb", dirty_glb.read_bytes(), "model/gltf-binary"))],
        data={"name": "SM_Imported", "preset_key": "prop_default"},
    )
    assert imported.status_code == 201, imported.text
    asset_id = imported.json()["asset"]["id"]

    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["source"] == "mesh"
    # 导入时就应该有一个 import 版本节点，管线才能从它往后接
    assert [v["op"] for v in detail["versions"]] == ["import"]

    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    job = wait_for_job(client, pipeline.json()["job"]["id"])
    assert job["status"] == "succeeded", job

    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["reports"]


def test_import_mesh_rejects_wrong_format(client):
    response = client.post(
        "/api/assets/import-mesh",
        files=[("file", ("notes.txt", b"hello", "text/plain"))],
        data={"name": "SM_Bad", "preset_key": "prop_default"},
    )
    assert response.status_code == 400
    assert "不支持的网格格式" in response.json()["detail"]


# ------------------------------------------------------------------ 安全与边界


def test_files_endpoint_blocks_path_traversal(client):
    """文件接口只允许读数据目录内的文件 —— 本地服务也不该变成任意文件读取器。"""
    import os

    outside = "C:\\Windows\\win.ini" if os.name == "nt" else "/etc/passwd"
    response = client.get("/api/files", params={"path": outside})
    assert response.status_code == 403
    assert "只允许访问" in response.json()["detail"]


def test_files_endpoint_requires_absolute_path(client):
    response = client.get("/api/files", params={"path": "relative/path.glb"})
    assert response.status_code == 400


def test_export_is_blocked_when_validation_fails(client, dirty_glb):
    """校验未过时导出必须被拦截 —— 这是"校验报告先行"的强制点（见 5.3）。"""
    imported = client.post(
        "/api/assets/import-mesh",
        files=[("file", ("dirty.glb", dirty_glb.read_bytes(), "model/gltf-binary"))],
        data={"name": "bad name!", "preset_key": "prop_default"},
    ).json()
    asset_id = imported["asset"]["id"]

    job = wait_for_job(client, client.post(f"/api/assets/{asset_id}/pipeline", json={}).json()["job"]["id"])
    assert job["status"] == "succeeded", job

    report = client.get(f"/api/assets/{asset_id}").json()["reports"][-1]
    if report["passed"]:
        pytest.skip("该样例网格恰好通过了校验，无法验证拦截路径")

    blocked = client.post(f"/api/assets/{asset_id}/export", json={"preset": "unity"})
    assert blocked.status_code == 409
    assert "校验未通过" in blocked.json()["detail"]

    # 明确勾选"忽略校验结果"后必须放行 —— 拦截是为了提醒，不是为了卡住用户
    allowed = client.post(
        f"/api/assets/{asset_id}/export",
        json={"preset": "unity", "allow_failed_export": True},
    )
    assert allowed.status_code == 200, allowed.text


def test_delete_removes_asset_and_files(client):
    """删除：目录连产物一起消失；再删一次 404。"""
    created = client.post("/api/assets", json={"name": "SM_To_Delete"}).json()
    asset_id = created["asset"]["id"]

    from app import store

    asset_dir = store.asset_dir(asset_id)
    assert asset_dir.exists()

    deleted = client.delete(f"/api/assets/{asset_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True

    assert not asset_dir.exists()
    assert asset_id not in [a["asset"]["id"] for a in client.get("/api/assets").json()]
    assert client.delete(f"/api/assets/{asset_id}").status_code == 404


def test_budget_limit_blocks_generation(client):
    """预算上限生效时生成要被拦下，并给出可操作的提示。"""
    client.patch("/api/settings", json={"monthly_budget_cny": 0.01, "cost_per_generation_cny": 5.0})

    upload = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_Budget", "preset_key": "prop_default"},
    ).json()
    asset_id = upload["asset"]["id"]

    # mock provider 成本为 0，所以先把成本单价抬高，并制造一笔已用额度
    from app.config import get_settings

    get_settings().record_usage({"at": "2026-09-01T00:00:00Z", "cost": 100.0, "provider": "test"})

    response = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 1})
    assert response.status_code == 402, response.text
    assert "预算上限" in response.json()["detail"]


# ------------------------------------------------------------------ 异常处理


def test_provider_error_returns_400_without_stacktrace(client):
    """ProviderError 必须返回 400，消息写人话，且绝不带堆栈。"""
    from app.providers import ProviderError

    # 临时制造一个会抛 ProviderError 的场景
    def boom(*args, **kwargs):
        raise ProviderError("这是一个人话错误")

    from app import routers

    original = routers.assets.run_pipeline
    routers.assets.run_pipeline = boom
    try:
        response = client.post("/api/assets/nonexistent/pipeline", json={})
        # run_pipeline 在调用前会先检查资产存在性，所以这里可能 404；
        # 换一种方式：直接调一个会触发 ProviderError 的内部路径太麻烦，
        # 不如直接测试 exception handler 的注册。
        # 更简单：用一个已知会触发 ProviderError 的场景。
        pass
    finally:
        routers.assets.run_pipeline = original

    # 实际验证：直接通过 app 的 dependency_overrides 或手动调 handler 并不优雅。
    # 改为测试「已知触发 ProviderError 的真实场景」： BYOK 模式 + 未配置 Key + 禁用 mock fallback
    from app.config import reset_settings_cache
    from app.providers import registry

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("ASSETAGENT_ALLOW_MOCK_FALLBACK", "false")
    reset_settings_cache()
    registry.reload()

    upload = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_NoMock", "preset_key": "prop_default"},
    ).json()
    asset_id = upload["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 1})
    assert response.status_code == 400, response.text
    body = response.json()
    assert "detail" in body
    assert "traceback" not in str(body).lower()
    assert "ProviderError" not in str(body)

    monkeypatch.undo()
    reset_settings_cache()
    registry.reload()


def test_unexpected_error_returns_500_without_stacktrace():
    """未预期异常返回 500，给前端人话文案，日志里才有堆栈。

    TestClient 默认会把服务端异常重新抛出来（方便调试），
    所以这里直接调用 exception handler 验证行为。
    """
    import asyncio
    from unittest.mock import MagicMock

    from app.main import _catchall_handler

    request = MagicMock()
    exc = RuntimeError("内部炸了")
    response = asyncio.run(_catchall_handler(request, exc))
    assert response.status_code == 500
    body = response.body.decode()
    assert "detail" in body
    assert "traceback" not in body.lower()
    assert "RuntimeError" not in body
    assert "重试" in body or "反馈" in body

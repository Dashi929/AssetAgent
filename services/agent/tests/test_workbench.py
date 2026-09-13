"""工作台改造测试：导入后台队列、2D 图片素材生成、AI 属性编辑。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.ai.llm as llm_module
from app import telemetry
from test_api import make_png_bytes, wait_for_job


def _glb_bytes() -> bytes:
    import numpy as np
    import trimesh

    mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.5)
    return mesh.export(file_type="glb")


# ------------------------------------------------------------------ 导入队列


def test_import_model_runs_pipeline_in_background(client):
    glb = _glb_bytes()
    response = client.post(
        "/api/assets/import",
        files=[("files", ("mush.glb", glb, "application/octet-stream"))],
        data={"name": "SM_Queue_Prop"},
    )
    assert response.status_code == 202, response.text
    imports = response.json()["imports"]
    assert len(imports) == 1
    assert imports[0]["kind"] == "model"

    job = wait_for_job(client, imports[0]["job"]["id"])
    assert job["status"] == "succeeded", job

    asset_id = imports[0]["asset"]["asset"]["id"]
    detail = client.get(f"/api/assets/{asset_id}").json()
    # 后台队列自动跑完整条管线
    assert detail["validation"] is not None
    assert detail["asset"]["kind"] == "model"
    assert detail["turntable"], "导入队列应产出转台帧"

    # 导入不限面数（2026-09-14）：不减面，head 保持源模型面数
    assert detail["asset"]["spec"]["face_budget"] is None
    assert not [v for v in detail["versions"] if v["op"] == "decimate"], "导入资产不应有减面版本"
    from app.tools.mesh_io import load_mesh

    head = next(v for v in detail["versions"] if v["id"] == detail["asset"]["head_version_id"])
    assert len(load_mesh(head["mesh_path"]).faces) == 320  # icosphere(subdivisions=2) 原样保留


def test_import_image_creates_2d_asset(client):
    response = client.post(
        "/api/assets/import",
        files=[("files", ("sprite.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_Sprite"},
    )
    assert response.status_code == 202, response.text
    imports = response.json()["imports"]
    assert imports[0]["kind"] == "image"

    job = wait_for_job(client, imports[0]["job"]["id"])
    assert job["status"] == "succeeded", job

    asset_id = imports[0]["asset"]["asset"]["id"]
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["kind"] == "image"
    assert detail["asset"]["status"] == "validated"
    # 2D 资产的缩略图 = 源图
    assert detail["thumbnail"]


def test_import_mixed_batch_and_unsupported(client):
    glb = _glb_bytes()
    response = client.post(
        "/api/assets/import",
        files=[
            ("files", ("a.glb", glb, "application/octet-stream")),
            ("files", ("b.png", make_png_bytes(), "image/png")),
            ("files", ("c.exe", b"garbage", "application/octet-stream")),
        ],
    )
    assert response.status_code == 202
    imports = response.json()["imports"]
    kinds = [i["kind"] for i in imports]
    assert kinds == ["model", "image", "unsupported"]
    assert "不支持" in imports[2]["error"]


def test_import_model_failure_marks_asset_failed(client):
    response = client.post(
        "/api/assets/import",
        files=[("files", ("bad.fbx", b"not-a-real-fbx", "application/octet-stream"))],
    )
    assert response.status_code == 202
    imports = response.json()["imports"]
    job = wait_for_job(client, imports[0]["job"]["id"])
    # FBX 转换失败 → 任务失败 + 资产标失败（而不是僵尸 PROCESSING）
    assert job["status"] == "failed"
    asset_id = imports[0]["asset"]["asset"]["id"]
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["status"] == "failed"


def test_job_queue_lists_recent_with_asset_name(client):
    """任务队列浮窗：/api/jobs/queue 返回最近任务并带资产名；路由不被 /{job_id} 吞掉。"""
    response = client.post(
        "/api/assets/import",
        files=[("files", ("sprite_q.png", make_png_bytes(), "image/png"))],
    )
    assert response.status_code == 202, response.text
    imports = response.json()["imports"]
    job_id = imports[0]["job"]["id"]
    asset_id = imports[0]["asset"]["asset"]["id"]
    wait_for_job(client, job_id)

    queue = client.get("/api/jobs/queue")
    assert queue.status_code == 200, queue.text
    rows = queue.json()["jobs"]
    row = next((j for j in rows if j["id"] == job_id), None)
    assert row is not None, f"刚完成的任务应出现在队列里：{rows}"
    assert row["status"] == "succeeded"
    assert row["asset_id"] == asset_id
    # 导入没传 name → 资产名 = 文件名去扩展名
    assert row["asset_name"] == "sprite_q"


# ------------------------------------------------------------------ 2D 生成


def _make_image_asset(client: TestClient) -> str:
    uploaded = client.post(
        "/api/assets",
        json={"name": "SM_Icon", "kind": "image", "prompt": "一枚像素风红药水图标"},
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["asset"]["id"]


def test_generate_image_requires_image_kind(client, with_llm_key):
    glb = _glb_bytes()
    imported = client.post(
        "/api/assets/import-mesh",
        files={"file": ("m.glb", glb, "application/octet-stream")},
        data={"name": "SM_Model"},
    )
    asset_id = imported.json()["asset"]["id"]
    response = client.post(f"/api/assets/{asset_id}/generate-image", json={"prompt": "x"})
    assert response.status_code == 400
    assert "2D" in response.json()["detail"]


def test_generate_image_flow(client, monkeypatch, with_llm_key):
    captured: dict = {}

    async def fake_chat_json(system, user_text, *, temperature=0.4):
        captured["system"] = system
        return {"prompt": "像素风红药水图标，纯色背景，单主体"}

    async def fake_generate_image(prompt, out_path, *, size="1024x1024"):
        captured["prompt"] = prompt
        Image.new("RGB", (64, 64), (180, 40, 40)).save(out_path)
        return out_path

    monkeypatch.setattr(llm_module, "chat_json", fake_chat_json)
    from app.ai import imagegen as imagegen_module

    monkeypatch.setattr(imagegen_module, "generate_image", fake_generate_image)

    asset_id = _make_image_asset(client)
    started = client.post(f"/api/assets/{asset_id}/generate-image", json={"prompt": "红药水图标"})
    assert started.status_code == 202, started.text
    job = wait_for_job(client, started.json()["job"]["id"])
    assert job["status"] == "succeeded", job

    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["status"] == "validated"
    # 图片落在版本节点（op=generate）并成为缩略图
    gen_nodes = [v for v in detail["versions"] if v["op"] == "generate"]
    assert gen_nodes and gen_nodes[-1]["params"]["image"]
    assert "药水" in captured["prompt"]
    assert detail["thumbnail"]

    events = telemetry.read_events()
    assert any(e.get("event") == "generate_image" for e in events)


def test_generate_image_on_model_asset_rejected(client):
    asset_id = _make_image_asset(client)
    # 3D 资产上调 generate-image 应 400
    glb = _glb_bytes()
    imported = client.post(
        "/api/assets/import-mesh",
        files={"file": ("m.glb", glb, "application/octet-stream")},
        data={"name": "SM_Model2"},
    )
    model_id = imported.json()["asset"]["id"]
    response = client.post(f"/api/assets/{model_id}/generate-image", json={"prompt": "x"})
    assert response.status_code == 400
    assert asset_id


# ------------------------------------------------------------------ AI 属性编辑


def test_ai_edit_requires_key(client):
    asset_id = _make_image_asset(client)
    response = client.post(f"/api/assets/{asset_id}/ai-edit", json={"instruction": "改名"})
    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def test_ai_edit_applies_whitelisted_changes(client, monkeypatch, with_llm_key):
    async def fake_chat_json(system, user_text, *, temperature=0.4):
        return {
            "changes": {
                "name": "SM_Sword_01",
                "tags": ["武器", "金属"],
                "spec": {"face_budget": 2000},
            },
            "summary": "改了名字、标签与面数预算",
        }

    monkeypatch.setattr(llm_module, "chat_json", fake_chat_json)

    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_Knife", "prompt": "一把小刀"},
    )
    asset_id = uploaded.json()["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/ai-edit", json={"instruction": "改成短剑资产并收紧预算"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"]["name"] == "SM_Sword_01"
    assert body["applied"]["spec"]["face_budget"] == 2000
    assert body["before"]["name"] == "SM_Knife"

    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["asset"]["name"] == "SM_Sword_01"
    assert detail["asset"]["spec"]["face_budget"] == 2000
    assert "武器" in detail["asset"]["tags"]

    events = telemetry.read_events()
    assert any(e.get("event") == "llm_edit" for e in events)


def test_ai_edit_rejects_invalid_name(client, monkeypatch, with_llm_key):
    async def fake_chat_json(system, user_text, *, temperature=0.4):
        return {"changes": {"name": "not valid name!!"}, "summary": ""}

    monkeypatch.setattr(llm_module, "chat_json", fake_chat_json)
    uploaded = client.post("/api/assets", json={"name": "SM_X", "kind": "image"})
    asset_id = uploaded.json()["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/ai-edit", json={"instruction": "改名"})
    assert response.status_code == 400
    assert "命名规范" in response.json()["detail"]
    # 资产名未被破坏
    assert client.get(f"/api/assets/{asset_id}").json()["asset"]["name"] == "SM_X"


def test_ai_edit_empty_changes_is_honest(client, monkeypatch, with_llm_key):
    async def fake_chat_json(system, user_text, *, temperature=0.4):
        return {"changes": {}, "summary": "没有可改的"}

    monkeypatch.setattr(llm_module, "chat_json", fake_chat_json)
    uploaded = client.post("/api/assets", json={"name": "SM_Y", "kind": "image"})
    asset_id = uploaded.json()["asset"]["id"]
    response = client.post(f"/api/assets/{asset_id}/ai-edit", json={"instruction": "随便看看"})
    assert response.status_code == 400
    assert "解析出" in response.json()["detail"]

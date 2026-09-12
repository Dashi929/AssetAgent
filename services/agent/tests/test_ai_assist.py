"""AI 助手（提示词优化 / 视觉校验）测试。

LLM 调用统一 monkeypatch —— 测试验证的是**管线逻辑**（参数校验、落盘、
优先级、报错语义），不是模型本身。Key 缺失必须 400、给人话。
"""

from __future__ import annotations

import pytest
from test_api import make_png_bytes, wait_for_job

import app.ai.llm as llm_module
from app import telemetry
from app.config import get_settings


@pytest.fixture
def with_llm_key(monkeypatch):
    """给测试进程塞一个 LLM Key（走真实 settings 机制）。"""
    settings = get_settings()
    settings.set_provider_key("llm", "test-key-12345")
    yield
    settings.set_provider_key("llm", "")


def test_enhance_requires_key(client, monkeypatch):
    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_KB_Prop", "prompt": "一把生锈的短剑"},
    )
    asset_id = uploaded.json()["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/enhance-prompt", json={"description": ""})
    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def test_enhance_uses_knowledge_base_and_stores_result(client, monkeypatch, with_llm_key):
    captured = {}

    async def fake_chat_json(system, user_text, *, temperature=0.4):
        captured["system"] = system
        captured["user"] = user_text
        return {
            "prompt": "一把生锈的矮人短剑，stylized 手绘风格，剑身有缺口与锈迹，木质缠绳剑柄，单手可持，约 60cm",
            "keywords": ["stylized", "rusty sword", "hand-painted", "game asset"],
            "negative_prompt": "文字、水印、透明材质",
            "rationale": "补充了风格词与比例锚点",
        }

    monkeypatch.setattr(llm_module, "chat_json", fake_chat_json)

    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_KB_Prop", "prompt": "一把生锈的短剑"},
    )
    asset_id = uploaded.json()["asset"]["id"]

    response = client.post(f"/api/assets/{asset_id}/enhance-prompt", json={"description": ""})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "短剑" in body["prompt"]
    assert body["keywords"][0] == "stylized"

    # 知识库被注入 system（TA 编辑的 recipes/knowledge/prompt_optimization.md）
    assert "知识库" in captured["system"]
    assert "面数预算" in captured["system"]  # 规格上下文被带上

    # 结果落盘 + generate 会优先使用优化词
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert "手绘风格" in detail["asset"]["enhanced_prompt"]

    events = telemetry.read_events()
    assert any(e.get("event") == "llm_enhance" for e in events)


def test_enhance_without_description_fails(client, monkeypatch, with_llm_key):
    uploaded = client.post(
        "/api/assets",
        json={"name": "SM_Empty_Prop"},
    )
    asset_id = uploaded.json()["asset"]["id"]
    response = client.post(f"/api/assets/{asset_id}/enhance-prompt", json={"description": ""})
    assert response.status_code == 400


def test_visual_check_flow(client, monkeypatch, with_llm_key):
    async def fake_vision(system, user_text, images, *, temperature=0.2):
        # 概念图在前、转台帧在后；至少有一张成品图
        assert images, "必须带图"
        captured["images"] = images
        captured["user"] = user_text
        return {
            "passed": True,
            "score": 82,
            "summary": "形体与描述一致，风格匹配。",
            "issues": [{"severity": "low", "message": "剑柄缠绳纹理略糊"}],
            "suggestions": ["下次生成强调剑格形状"],
        }

    captured: dict = {}
    monkeypatch.setattr(llm_module, "vision_chat_json", fake_vision)

    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_QA_Prop", "prompt": "一把生锈的短剑"},
    )
    asset_id = uploaded.json()["asset"]["id"]

    gen = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 1})
    assert wait_for_job(client, gen.json()["job"]["id"])["status"] == "succeeded"
    detail = client.get(f"/api/assets/{asset_id}").json()
    client.post(f"/api/assets/{asset_id}/pick", json={"variant_id": sorted(v["id"] for v in detail["variants"])[0]})
    pipe = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert wait_for_job(client, pipe.json()["job"]["id"])["status"] == "succeeded"

    response = client.post(f"/api/assets/{asset_id}/visual-check")
    assert response.status_code == 200, response.text
    check = response.json()["check"]
    assert check["passed"] is True
    assert check["score"] == 82
    assert check["issues"][0]["severity"] == "low"
    assert any("turntable" in p for p in captured["images"])

    # 结论落盘并可从详情读回
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["semantic_checks"][-1]["score"] == 82

    events = telemetry.read_events()
    assert any(e.get("event") == "llm_visual_check" for e in events)


def test_visual_check_requires_version(client, monkeypatch, with_llm_key):
    uploaded = client.post(
        "/api/assets/upload",
        files=[("files", ("c.png", make_png_bytes(), "image/png"))],
        data={"name": "SM_NoVersion"},
    )
    asset_id = uploaded.json()["asset"]["id"]
    response = client.post(f"/api/assets/{asset_id}/visual-check")
    assert response.status_code == 400


def test_llm_settings_roundtrip_and_probe(client):
    # 未配置 Key：探活返回 ok=False 而不是 500
    probe = client.post("/api/settings/llm/test")
    assert probe.status_code == 200
    assert probe.json()["ok"] is False

    # 配置（走运行时覆盖 + secrets），掩码回显、明文不落响应
    secret = "sk-llm-secret-9876"
    saved = client.put("/api/settings/llm", json={"model": "glm-4-plus", "key": secret})
    assert saved.status_code == 200
    assert secret not in saved.text
    snapshot = saved.json()
    assert snapshot["llm"]["model"] == "glm-4-plus"
    assert snapshot["llm"]["configured"] is True
    assert snapshot["llm"]["key_masked"].endswith("9876")

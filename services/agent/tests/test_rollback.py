"""版本树回滚测试。

回滚语义：**非破坏性** —— 把目标版本的网格复制成新的 head 节点（op=rollback），
不删任何历史。导出永远用 head，所以"回滚后导出"就是拿旧版本的产物出活。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_api import wait_for_job


def _import_and_pipeline(client: TestClient, dirty_glb) -> str:
    response = client.post(
        "/api/assets/import-mesh",
        files={"file": ("dirty.glb", dirty_glb.read_bytes(), "model/gltf-binary")},
        data={"name": "回滚样例"},
    )
    assert response.status_code == 201, response.text
    asset_id = response.json()["asset"]["id"]

    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    job = wait_for_job(client, pipeline.json()["job"]["id"])
    assert job["status"] == "succeeded", job
    return asset_id


def test_rollback_creates_new_head_without_touching_history(client, dirty_glb):
    asset_id = _import_and_pipeline(client, dirty_glb)
    before = client.get(f"/api/assets/{asset_id}").json()
    before_versions = before["versions"]
    assert len(before_versions) >= 2  # import + 管线节点

    # 回滚到最早的 import 节点
    import_node = min(before_versions, key=lambda v: v["created_at"])
    rolled = client.post(f"/api/assets/{asset_id}/rollback", json={"version_id": import_node["id"]})
    assert rolled.status_code == 200, rolled.text

    after = client.get(f"/api/assets/{asset_id}").json()
    # 历史只增不改：旧节点原样都在，多了一个 rollback 节点
    assert len(after["versions"]) == len(before_versions) + 1
    assert {v["id"] for v in before_versions} <= {v["id"] for v in after["versions"]}

    rollback_nodes = [v for v in after["versions"] if v["op"] == "rollback"]
    assert len(rollback_nodes) == 1
    node = rollback_nodes[0]
    assert node["parent_id"] == import_node["id"]
    assert node["params"]["rolled_back_from"] == import_node["id"]
    # head 指向回滚节点，网格文件真实存在
    assert after["asset"]["head_version_id"] == node["id"]
    from pathlib import Path

    assert Path(node["mesh_path"]).exists()
    # 回滚后资产回到 PROCESSING（可导出/重跑管线）
    assert after["asset"]["status"] == "processing"


def test_rollback_copies_obj_measurement_file(client, dirty_glb):
    """OBJ 副本（四边面/n-gon 规则的测量文件）要跟过来，否则规则测量退回 GLB。"""
    asset_id = _import_and_pipeline(client, dirty_glb)
    detail = client.get(f"/api/assets/{asset_id}").json()
    source_with_obj = next(
        v for v in detail["versions"] if v["stats"].get("obj_path")
    )
    rolled = client.post(
        f"/api/assets/{asset_id}/rollback", json={"version_id": source_with_obj["id"]}
    )
    assert rolled.status_code == 200, rolled.text

    after = client.get(f"/api/assets/{asset_id}").json()
    rollback_node = next(v for v in after["versions"] if v["op"] == "rollback")
    assert rollback_node["stats"].get("obj_path")
    from pathlib import Path

    assert Path(rollback_node["stats"]["obj_path"]).exists()
    # obj_path 必须指向回滚节点自己的目录，不能还指着旧版本
    assert str(source_with_obj["id"]) not in rollback_node["stats"]["obj_path"]


def test_rollback_then_export_uses_rolled_back_mesh(client, dirty_glb):
    asset_id = _import_and_pipeline(client, dirty_glb)
    detail = client.get(f"/api/assets/{asset_id}").json()
    import_node = min(detail["versions"], key=lambda v: v["created_at"])

    rolled = client.post(f"/api/assets/{asset_id}/rollback", json={"version_id": import_node["id"]})
    assert rolled.status_code == 200, rolled.text

    exported = client.post(
        f"/api/assets/{asset_id}/export", json={"preset": "unity", "allow_failed_export": True}
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["export"]["version_id"] == client.get(f"/api/assets/{asset_id}").json()[
        "asset"
    ]["head_version_id"]


def test_rollback_unknown_version_returns_404(client, dirty_glb):
    asset_id = _import_and_pipeline(client, dirty_glb)
    response = client.post(f"/api/assets/{asset_id}/rollback", json={"version_id": "ver_nonexistent"})
    assert response.status_code == 404


def test_rollback_missing_mesh_returns_409(client, dirty_glb):
    """版本元数据在但网格文件丢了：给出明确的人话，而不是 500。"""
    asset_id = _import_and_pipeline(client, dirty_glb)
    detail = client.get(f"/api/assets/{asset_id}").json()
    victim = detail["versions"][0]
    import os

    os.remove(victim["mesh_path"])

    response = client.post(f"/api/assets/{asset_id}/rollback", json={"version_id": victim["id"]})
    assert response.status_code == 409
    assert "丢失" in response.json()["detail"]


def test_rollback_on_missing_asset_returns_404(client):
    response = client.post("/api/assets/ast_nonexistent/rollback", json={"version_id": "ver_x"})
    assert response.status_code == 404

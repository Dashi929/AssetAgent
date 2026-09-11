"""G4 压测：连续 20 次任务无崩溃、无 P0/P1（Release Gate）。

在 in-process TestClient 里交替跑两种任务各 10 次：
- 工作流 A：上传概念图 → 生成（mock）→ 挑选 → 管线
- 工作流 C：导入网格 → 管线
每次任务结束断言状态机推进正常、产物齐全。20 次全绿 = 逻辑层压测通过；
进程级稳定性（渲染进程崩溃、sidecar 段错误）仍以打包版真机复测为准。

用法（services/agent/ 下）：
    .venv/Scripts/python -m tests.stress_g4          # 20 连跑
    .venv/Scripts/python -m tests.stress_g4 --rounds 5
"""

from __future__ import annotations

import argparse
import io
import sys
import time

from fastapi.testclient import TestClient
from PIL import Image


def make_png_bytes(size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (200, 198, 190)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_dirty_glb() -> bytes:
    import numpy as np
    import trimesh

    base = trimesh.creation.icosphere(subdivisions=3, radius=0.5)
    stray = trimesh.creation.box(extents=(0.05, 0.05, 0.05))
    stray.apply_translation([0.9, 0.0, 0.03])
    merged = trimesh.util.concatenate([base, stray])
    faces = np.vstack([merged.faces, np.array([[0, 0, 1]], dtype=np.int64)])
    mesh = trimesh.Trimesh(vertices=merged.vertices, faces=faces, process=False)
    return mesh.export(file_type="glb")


def wait_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last["status"] in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} timeout")


def run_round(client: TestClient, index: int, mesh_bytes: bytes) -> dict:
    started = time.monotonic()
    if index % 2 == 0:
        uploaded = client.post(
            "/api/assets/upload",
            files=[("files", ("concept.png", make_png_bytes(), "image/png"))],
            data={"name": f"SM_StressA_{index:02d}"},
        )
        assert uploaded.status_code == 201, uploaded.text
        asset_id = uploaded.json()["asset"]["id"]
        gen = client.post(f"/api/assets/{asset_id}/generate", json={"num_variants": 2})
        assert gen.status_code == 202, gen.text
        assert wait_job(client, gen.json()["job"]["id"])["status"] == "succeeded"
        detail = client.get(f"/api/assets/{asset_id}").json()
        variant_id = sorted(v["id"] for v in detail["variants"])[0]
        picked = client.post(f"/api/assets/{asset_id}/pick", json={"variant_id": variant_id})
        assert picked.status_code == 200, picked.text
    else:
        uploaded = client.post(
            "/api/assets/import-mesh",
            files={"file": ("dirty.glb", mesh_bytes, "model/gltf-binary")},
            data={"name": f"SM_StressC_{index:02d}"},
        )
        assert uploaded.status_code == 201, uploaded.text
        asset_id = uploaded.json()["asset"]["id"]

    pipeline = client.post(f"/api/assets/{asset_id}/pipeline", json={})
    assert pipeline.status_code == 202, pipeline.text
    job = wait_job(client, pipeline.json()["job"]["id"])
    assert job["status"] == "succeeded", job

    detail = client.get(f"/api/assets/{asset_id}").json()
    assert detail["turntable"], "转台帧缺失"
    assert detail["validation"]["passed"], f"校验未过：{detail['validation']['failed_rules']}"
    exported = client.post(
        f"/api/assets/{asset_id}/export", json={"preset": "unity", "allow_failed_export": True}
    )
    assert exported.status_code == 200, exported.text
    return {"asset_id": asset_id, "seconds": round(time.monotonic() - started, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description="G4 压测：连续任务")
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()

    # 独立数据目录，不碰开发库
    import os
    import tempfile

    os.environ["ASSETAGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="assetagent-g4-")
    os.environ["ASSETAGENT_ALLOW_MOCK_FALLBACK"] = "true"
    os.environ["ASSETAGENT_ROUTE_MODE"] = "byok"
    for key in ("MESHY_API_KEY", "TRIPO_API_KEY", "RODIN_API_KEY", "HUNYUAN3D_API_KEY"):
        os.environ.pop(key, None)

    mesh_bytes = make_dirty_glb()
    failures: list[str] = []
    total_seconds = 0.0

    print("=" * 68)
    print(f"G4 压测：连续 {args.rounds} 轮任务（工作流 A/C 交替）")
    print("=" * 68)

    from app.config import reset_settings_cache
    from app.providers import registry

    reset_settings_cache()
    registry.reload()

    with TestClient(app_client()) as client:
        for index in range(1, args.rounds + 1):
            try:
                result = run_round(client, index, mesh_bytes)
                total_seconds += result["seconds"]
                print(f"[{index:02d}/{args.rounds}] ✅ asset={result['asset_id']} {result['seconds']}s")
            except AssertionError as exc:
                failures.append(f"round {index}: {exc}")
                print(f"[{index:02d}/{args.rounds}] ❌ {exc}")
            except Exception as exc:  # 崩溃类异常也记下继续
                failures.append(f"round {index}: {type(exc).__name__}: {exc}")
                print(f"[{index:02d}/{args.rounds}] 💥 {type(exc).__name__}: {exc}")

    print("=" * 68)
    if failures:
        print(f"❌ {len(failures)}/{args.rounds} 轮失败：")
        for line in failures:
            print("   -", line)
        return 1
    print(f"✅ {args.rounds}/{args.rounds} 轮全部通过，总耗时 {total_seconds:.0f}s（均 {total_seconds / args.rounds:.1f}s/轮）")
    return 0


def app_client():
    from app.main import app

    return app


if __name__ == "__main__":
    sys.exit(main())

"""Meshy Provider（云端 image/text-to-3D）。

⚠️ 端点与字段以官方文档为准。**W1 需要对照官方文档校正一次**，所以把它们集中放在
下面的常量区，改的时候只改这一处。校正前不要在正式环境依赖它。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult, image_to_data_uri
from .polling import dig, download, poll_task

# ---- 端点（改这里） ----
IMAGE_TO_3D_PATH = "/openapi/v1/image-to-3d"
TEXT_TO_3D_PATH = "/openapi/v1/text-to-3d"
TASK_PATH = "/openapi/v1/{kind}/{task_id}"
AI_MODEL = "meshy-4"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0


class MeshyProvider(Gen3DProvider):
    name = "meshy"
    display_name = "Meshy"
    capabilities = ("image_to_3d", "text_to_3d", "pbr_texture", "remesh")
    note = "API 最全的一家，MVP 首选。端点需 W1 对照官方文档校正。"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._require_key()}"}

    def _payload(self, req: GenerateRequest, image: Path | None) -> dict:
        spec = req.spec
        payload: dict = {
            "enable_pbr": True,
            "should_remesh": True,
            "topology": "triangle",  # 四边面由我们自己的管线做，这里要三角面
            "ai_model": AI_MODEL,
        }
        if spec:
            payload["target_polycount"] = spec.face_budget
        if image is not None:
            payload["image_url"] = image_to_data_uri(image)
        if req.prompt:
            payload["prompt"] = req.prompt
        return payload

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        headers = self._headers()
        images = list(req.image_paths)
        kind = "image-to-3d" if images else "text-to-3d"
        create_path = IMAGE_TO_3D_PATH if images else TEXT_TO_3D_PATH

        if not images and not req.prompt.strip():
            raise ProviderError("Meshy 需要一张概念图或一段文字描述，两者都没有无法生成。")

        results: list[VariantResult] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as client:
            for index in range(max(1, req.num_variants)):
                # 多变体：有多视图就轮换参考图，没有就靠 prompt 变化
                image = images[index % len(images)] if images else None
                payload = self._payload(req, image)
                req.report(
                    index / max(1, req.num_variants) * 0.5,
                    f"提交第 {index + 1}/{req.num_variants} 个变体…",
                )

                created = await client.post(create_path, headers=headers, json=payload)
                if created.status_code >= 400:
                    raise ProviderError(
                        f"Meshy 提交任务失败（HTTP {created.status_code}）：{created.text[:200]}"
                    )
                task_id = dig(created.json(), "result") or dig(created.json(), "id")
                if not task_id:
                    raise ProviderError(f"Meshy 未返回任务 ID：{created.text[:200]}")

                query_url = TASK_PATH.format(kind=kind, task_id=task_id)
                state = await poll_task(
                    client,
                    query_url,
                    headers,
                    status_path="status",
                    interval=POLL_INTERVAL,
                    timeout=POLL_TIMEOUT,
                    on_tick=lambda _n, s, i=index: req.report(
                        0.5 + (i + 0.5) / max(1, req.num_variants) * 0.4,
                        f"变体 {i + 1} 生成中（{s or '排队'}）…",
                    ),
                )

                urls = dig(state, "model_urls", {}) or {}
                url = urls.get("glb") or urls.get("gltf") or urls.get("fbx")
                if not url:
                    raise ProviderError("Meshy 任务完成但没有返回模型下载地址。")

                suffix = Path(url.split("?")[0]).suffix or ".glb"
                target = req.out_dir / f"variant_{index + 1:02d}{suffix}"
                await download(client, url, target)

                results.append(
                    VariantResult(
                        provider=self.name,
                        mesh_path=target,
                        params={
                            "task_id": task_id,
                            "mode": kind,
                            "prompt": req.prompt,
                            "reference_image": str(image) if image else None,
                            "spec": self._spec_dict(req),
                        },
                        raw={"model_urls": urls, "thumbnail_url": dig(state, "thumbnail_url")},
                    )
                )
                req.report(
                    (index + 1) / max(1, req.num_variants) * 0.9,
                    f"已完成 {index + 1}/{req.num_variants} 个变体",
                )

        return results

    async def healthcheck(self) -> bool:
        if not self.has_key:
            return False
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
                # 用一个不存在的任务 ID 探活：401/403 = Key 无效，404 = Key 有效但任务不存在
                response = await client.get(
                    TASK_PATH.format(kind="image-to-3d", task_id="healthcheck"),
                    headers=self._headers(),
                )
            return response.status_code not in (401, 403)
        except httpx.HTTPError:
            return False


__all__ = ["MeshyProvider"]

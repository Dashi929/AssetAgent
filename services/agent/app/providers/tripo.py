"""Tripo Provider（云端 image/text-to-3D）。

⚠️ 端点与字段以官方文档为准，**W1 需对照校正**。常量集中在下面。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult
from .polling import dig, download, poll_task

# ---- 端点（改这里） ----
CREATE_PATH = "/v2/openapi/task"
QUERY_PATH = "/v2/openapi/task/{task_id}"
MODEL_VERSION = "v2.5-20250123"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0


class TripoProvider(Gen3DProvider):
    name = "tripo"
    display_name = "Tripo"
    capabilities = ("image_to_3d", "text_to_3d", "pbr_texture")
    note = "接入简单，适合作为 Meshy 的对照。端点需 W1 对照官方文档校正。"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._require_key()}"}

    def _payload(self, req: GenerateRequest, image: Path | None) -> dict:
        if image is not None:
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            suffix = image.suffix.lstrip(".").lower() or "png"
            return {
                "type": "image_to_model",
                "model_version": MODEL_VERSION,
                "file": {"type": suffix, "data": encoded},
            }
        return {
            "type": "text_to_model",
            "model_version": MODEL_VERSION,
            "prompt": req.prompt,
        }

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        headers = self._headers()
        images = list(req.image_paths)
        if not images and not req.prompt.strip():
            raise ProviderError("Tripo 需要一张概念图或一段文字描述，两者都没有无法生成。")

        results: list[VariantResult] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as client:
            for index in range(max(1, req.num_variants)):
                image = images[index % len(images)] if images else None
                req.report(index / max(1, req.num_variants) * 0.5, f"提交变体 {index + 1}…")

                created = await client.post(CREATE_PATH, headers=headers, json=self._payload(req, image))
                if created.status_code >= 400:
                    raise ProviderError(
                        f"Tripo 提交任务失败（HTTP {created.status_code}）：{created.text[:200]}"
                    )
                body = created.json()
                if dig(body, "code", 0) not in (0, None):
                    raise ProviderError(f"Tripo 返回错误：{dig(body, 'message') or created.text[:200]}")
                task_id = dig(body, "data.task_id")
                if not task_id:
                    raise ProviderError(f"Tripo 未返回任务 ID：{created.text[:200]}")

                state = await poll_task(
                    client,
                    QUERY_PATH.format(task_id=task_id),
                    headers,
                    status_path="data.status",
                    interval=POLL_INTERVAL,
                    timeout=POLL_TIMEOUT,
                    on_tick=lambda _n, s, i=index: req.report(
                        0.5 + (i + 0.5) / max(1, req.num_variants) * 0.4,
                        f"变体 {i + 1} 生成中（{s or '排队'}）…",
                    ),
                )

                url = (
                    dig(state, "data.output.pbr_model")
                    or dig(state, "data.output.model")
                    or dig(state, "data.output.base_model")
                )
                if not url:
                    raise ProviderError("Tripo 任务完成但没有返回模型下载地址。")

                suffix = Path(url.split("?")[0]).suffix or ".glb"
                target = req.out_dir / f"variant_{index + 1:02d}{suffix}"
                await download(client, url, target)

                results.append(
                    VariantResult(
                        provider=self.name,
                        mesh_path=target,
                        params={
                            "task_id": task_id,
                            "model_version": MODEL_VERSION,
                            "prompt": req.prompt,
                            "reference_image": str(image) if image else None,
                            "spec": self._spec_dict(req),
                        },
                        raw={"output": dig(state, "data.output", {})},
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
                response = await client.get(
                    QUERY_PATH.format(task_id="healthcheck"), headers=self._headers()
                )
            return response.status_code not in (401, 403)
        except httpx.HTTPError:
            return False


__all__ = ["TripoProvider"]

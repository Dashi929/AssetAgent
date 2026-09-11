"""Rodin / Hyper3D Provider（云端 image-to-3D）。

⚠️ 端点与字段以官方文档为准，**W1 需对照校正**。

注意这家和另外两家不一样：**状态查询是 POST 不是 GET**，所以没走共用的 poll_task。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult, image_to_data_uri
from .polling import dig, download

# ---- 端点（改这里） ----
CREATE_PATH = "/api/v2/rodin"
STATUS_PATH = "/api/v2/status"

QUALITY = "medium"
GEOMETRY_FORMAT = "glb"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0

DONE = {"done", "succeeded", "success", "completed"}
FAILED = {"failed", "error", "cancelled", "canceled"}


class RodinProvider(Gen3DProvider):
    name = "rodin"
    display_name = "Rodin (Hyper3D)"
    capabilities = ("image_to_3d", "pbr_texture", "high_quality")
    note = "质量上限高的一家，适合做盲测对照。端点需 W1 对照官方文档校正。"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._require_key()}"}

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        headers = self._headers()
        images = list(req.image_paths)
        if not images:
            raise ProviderError("Rodin 目前只支持以概念图作为输入，请先拖入一张图。")

        results: list[VariantResult] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as client:
            for index in range(max(1, req.num_variants)):
                image = images[index % len(images)]
                payload = {
                    "images": [image_to_data_uri(image)],
                    "prompt": req.prompt,
                    "geometry_file_format": GEOMETRY_FORMAT,
                    "material": "PBR",
                    "quality": QUALITY,
                    "tier": "Regular",
                }
                req.report(index / max(1, req.num_variants) * 0.5, f"提交变体 {index + 1}…")

                created = await client.post(CREATE_PATH, headers=headers, json=payload)
                if created.status_code >= 400:
                    raise ProviderError(
                        f"Rodin 提交任务失败（HTTP {created.status_code}）：{created.text[:200]}"
                    )
                body = created.json()
                subscription_key = dig(body, "uuid") or dig(body, "jobs.uuids.0")
                if not subscription_key:
                    raise ProviderError(f"Rodin 未返回任务标识：{created.text[:200]}")

                state = await self._await_job(client, headers, subscription_key, req, index)

                urls = dig(state, "urls", {}) or {}
                url = urls.get("glb") or urls.get("gltf") or urls.get("obj")
                if not url:
                    raise ProviderError("Rodin 任务完成但没有返回模型下载地址。")

                target = req.out_dir / f"variant_{index + 1:02d}.glb"
                await download(client, url, target)

                results.append(
                    VariantResult(
                        provider=self.name,
                        params={
                            "subscription_key": subscription_key,
                            "prompt": req.prompt,
                            "reference_image": str(image),
                            "spec": self._spec_dict(req),
                        },
                        mesh_path=target,
                        raw={"urls": urls},
                    )
                )
                req.report(
                    (index + 1) / max(1, req.num_variants) * 0.9,
                    f"已完成 {index + 1}/{req.num_variants} 个变体",
                )

        return results

    async def _await_job(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        subscription_key: str,
        req: GenerateRequest,
        index: int,
    ) -> dict:
        started = time.monotonic()
        while True:
            response = await client.post(
                STATUS_PATH, headers=headers, json={"subscription_key": subscription_key}
            )
            if response.status_code >= 400:
                raise ProviderError(
                    f"查询 Rodin 任务失败（HTTP {response.status_code}）：{response.text[:200]}"
                )
            job = dig(response.json(), "jobs.0", {}) or {}
            status = str(job.get("status", "")).lower()
            req.report(
                0.5 + (index + 0.5) / max(1, req.num_variants) * 0.4,
                f"变体 {index + 1} 生成中（{status or '排队'}）…",
            )
            if status in DONE:
                return job
            if status in FAILED:
                message = job.get("message") or status
                raise ProviderError(f"Rodin 生成失败：{message}")
            if time.monotonic() - started > POLL_TIMEOUT:
                raise ProviderError(f"Rodin 生成超时（已等待 {int(POLL_TIMEOUT)} 秒）。")
            await asyncio.sleep(POLL_INTERVAL)

    async def healthcheck(self) -> bool:
        if not self.has_key:
            return False
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
                response = await client.post(
                    STATUS_PATH, headers=self._headers(), json={"subscription_key": "healthcheck"}
                )
            return response.status_code not in (401, 403)
        except httpx.HTTPError:
            return False


__all__ = ["RodinProvider"]

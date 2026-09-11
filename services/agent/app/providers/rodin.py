"""Rodin / Hyper3D Provider（云端 image-to-3D）。

端点与流程已对照官方代码（2026-09-12，DeemosTech/rodin-api-mcp，Rodin 官方出品）：
- 基址 https://hyperhuman.deemos.com（api.hyper3d.com 是旧的 commerce 域名）
- 创建 POST /api/v2/rodin，**multipart 表单**：参数以表单字段传，图片作为
  "images" 文件字段（支持最多 5 张，第一张用于材质生成）
- 响应 {uuid, jobs: {uuids, subscription_key}}
- 查询 POST /api/v2/status，**表单字段** subscription_key；
  jobs[].status ∈ Done/Failed/Canceled（大小写不敏感处理后匹配）
- 拿下载地址 POST /api/v2/download，表单字段 task_uuid；
  响应 {list: [{name, url}]}，按名字挑 .glb
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult
from .polling import dig, download

# ---- 端点（改这里） ----
CREATE_PATH = "/api/v2/rodin"
STATUS_PATH = "/api/v2/status"
DOWNLOAD_PATH = "/api/v2/download"

QUALITY = "medium"
GEOMETRY_FORMAT = "glb"
POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0

DONE = {"done"}
FAILED = {"failed", "canceled"}


class RodinProvider(Gen3DProvider):
    name = "rodin"
    display_name = "Rodin (Hyper3D)"
    capabilities = ("image_to_3d", "pbr_texture", "high_quality")
    note = "质量上限高的一家，适合做盲测对照。端点已对照官方文档校正（2026-09-12）。"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._require_key()}"}

    def _multipart(self, req: GenerateRequest, image: Path) -> list:
        """官方客户端用 multipart 表单传参（custom_types.convert_to_files 同款字段）。"""
        fields: list = [
            ("condition_mode", (None, "concat")),
            ("geometry_file_format", (None, GEOMETRY_FORMAT)),
            ("material", (None, "PBR")),
            ("quality", (None, QUALITY)),
            ("tier", (None, "Regular")),
            ("mesh_mode", (None, "Raw")),  # Raw = 三角面；四边面交给自家管线处理
        ]
        if req.prompt.strip():
            fields.append(("prompt", (None, req.prompt)))
        fields.append(
            ("images", (image.name, image.read_bytes(), "application/octet-stream"))
        )
        return fields

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        headers = self._headers()
        images = list(req.image_paths)
        if not images:
            raise ProviderError("Rodin 目前只支持以概念图作为输入，请先拖入一张图。")

        results: list[VariantResult] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as client:
            for index in range(max(1, req.num_variants)):
                image = images[index % len(images)]
                req.report(index / max(1, req.num_variants) * 0.5, f"提交变体 {index + 1}…")

                created = await client.post(
                    CREATE_PATH, headers=headers, files=self._multipart(req, image)
                )
                if created.status_code >= 400:
                    raise ProviderError(
                        f"Rodin 提交任务失败（HTTP {created.status_code}）：{created.text[:200]}"
                    )
                body = created.json()
                subscription_key = dig(body, "jobs.subscription_key") or dig(body, "uuid")
                task_uuid = dig(body, "uuid") or dig(body, "jobs.uuids.0")
                if not subscription_key or not task_uuid:
                    raise ProviderError(f"Rodin 未返回任务标识：{created.text[:200]}")

                await self._await_job(client, headers, subscription_key, req, index)
                url = await self._pick_download_url(client, headers, str(task_uuid))
                if not url:
                    raise ProviderError("Rodin 任务完成但没有返回模型下载地址。")

                target = req.out_dir / f"variant_{index + 1:02d}.glb"
                await download(client, url, target)

                results.append(
                    VariantResult(
                        provider=self.name,
                        params={
                            "task_uuid": str(task_uuid),
                            "prompt": req.prompt,
                            "reference_image": str(image),
                            "spec": self._spec_dict(req),
                        },
                        mesh_path=target,
                        raw={"task_uuid": task_uuid},
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
    ) -> None:
        started = time.monotonic()
        while True:
            response = await client.post(
                STATUS_PATH, headers=headers, data={"subscription_key": subscription_key}
            )
            if response.status_code >= 400:
                raise ProviderError(
                    f"查询 Rodin 任务失败（HTTP {response.status_code}）：{response.text[:200]}"
                )
            statuses = [
                str(job.get("status", "")).lower()
                for job in (response.json().get("jobs") or [])
                if isinstance(job, dict)
            ]
            status = statuses[0] if statuses else ""
            req.report(
                0.5 + (index + 0.5) / max(1, req.num_variants) * 0.4,
                f"变体 {index + 1} 生成中（{status or '排队'}）…",
            )
            if any(s in FAILED for s in statuses):
                raise ProviderError("Rodin 生成失败。")
            if statuses and all(s in DONE for s in statuses):
                return
            if time.monotonic() - started > POLL_TIMEOUT:
                raise ProviderError(f"Rodin 生成超时（已等待 {int(POLL_TIMEOUT)} 秒）。")
            await asyncio.sleep(POLL_INTERVAL)

    async def _pick_download_url(
        self, client: httpx.AsyncClient, headers: dict[str, str], task_uuid: str
    ) -> str | None:
        response = await client.post(
            DOWNLOAD_PATH, headers=headers, data={"task_uuid": task_uuid}
        )
        if response.status_code >= 400:
            raise ProviderError(
                f"获取 Rodin 下载地址失败（HTTP {response.status_code}）：{response.text[:200]}"
            )
        entries = dig(response.json(), "list", []) or []
        urls = {
            str(entry.get("name") or ""): entry.get("url")
            for entry in entries
            if isinstance(entry, dict)
        }
        # 按名字挑 GLB（官方列表里模型文件名为 model.glb，另有预览图等）
        for name, url in urls.items():
            if name.lower().endswith(".glb") and url:
                return str(url)
        return next((str(u) for u in urls.values() if u), None)

    async def healthcheck(self) -> bool:
        if not self.has_key:
            return False
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
                response = await client.post(
                    STATUS_PATH,
                    headers=self._headers(),
                    data={"subscription_key": "healthcheck"},
                )
            return response.status_code not in (401, 403)
        except httpx.HTTPError:
            return False


__all__ = ["RodinProvider"]

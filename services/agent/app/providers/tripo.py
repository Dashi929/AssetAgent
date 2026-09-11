"""Tripo Provider（云端 image/text-to-3D）。

端点与字段已对照官方 Python SDK（2026-09-12，PyPI `tripo` 0.2.1 官方客户端）：
- 基址 https://api.tripo3d.ai/v2/openapi；创建 POST /task，查询 GET /task/{id}
- 鉴权 Bearer；响应 code != 0 即错误；data.task_id；status ∈ queued/running/succeeded/failed 等
- 图片官方推荐先 POST /upload（multipart）拿 image_token，任务里传 file={type, file_token}
- 产物在 data.output.{pbr_model, model, base_model}；model_version 合法值含 v2.5-20250123
- 探活用 GET /user/balance（官方 SDK 的 get_balance）
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult
from .polling import dig, download, poll_task

# ---- 端点（改这里） ----
CREATE_PATH = "/v2/openapi/task"
QUERY_PATH = "/v2/openapi/task/{task_id}"
UPLOAD_PATH = "/v2/openapi/upload"
BALANCE_PATH = "/v2/openapi/user/balance"
MODEL_VERSION = "v2.5-20250123"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0


class TripoProvider(Gen3DProvider):
    name = "tripo"
    display_name = "Tripo"
    capabilities = ("image_to_3d", "text_to_3d", "pbr_texture")
    note = "接入简单，适合作为 Meshy 的对照。端点已对照官方文档校正（2026-09-12）。"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._require_key()}"}

    def _payload(self, req: GenerateRequest, file_token: tuple[str, str] | None) -> dict:
        if file_token is not None:
            file_type, token = file_token
            return {
                "type": "image_to_model",
                "model_version": MODEL_VERSION,
                "pbr": True,
                "file": {"type": file_type, "file_token": token},
            }
        return {
            "type": "text_to_model",
            "model_version": MODEL_VERSION,
            "pbr": True,
            "prompt": req.prompt,
        }

    async def _upload_image(self, client: httpx.AsyncClient, image: Path) -> tuple[str, str]:
        """先传图拿 file_token —— 官方 SDK 的标准流程，比 base64 内嵌稳。"""
        suffix = image.suffix.lstrip(".").lower() or "png"
        with image.open("rb") as fh:
            response = await client.post(
                UPLOAD_PATH, headers=self._headers(), files={"file": (image.name, fh)}
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"Tripo 上传参考图失败（HTTP {response.status_code}）：{response.text[:200]}"
            )
        body = response.json()
        if dig(body, "code", 0) not in (0, None):
            raise ProviderError(f"Tripo 上传失败：{dig(body, 'message') or response.text[:200]}")
        token = dig(body, "data.image_token")
        if not token:
            raise ProviderError(f"Tripo 上传成功但未返回 image_token：{response.text[:200]}")
        return suffix, str(token)

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

                file_token = await self._upload_image(client, image) if image is not None else None
                created = await client.post(CREATE_PATH, headers=headers, json=self._payload(req, file_token))
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
                # 官方余额接口：Key 有效必 200；401/403 = Key 无效
                response = await client.get(BALANCE_PATH, headers=self._headers())
            return response.status_code not in (401, 403)
        except httpx.HTTPError:
            return False


__all__ = ["TripoProvider"]

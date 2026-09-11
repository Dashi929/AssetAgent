"""腾讯混元生3D Provider（云端 image/text-to-3D）。

腾讯云「混元生3D」（产品代码 ai3d）是三家人可自由选用（Meshy / Tripo / 混元3D，
2026-09-11 拍板，见产品策划文档 12）里的国产选项：国内访问稳定、按量计费人民币结算。

⚠️ 端点与字段以官方文档为准，**W1 需对照校正**（产品 1804：
https://cloud.tencent.com/document/product/1804）。已从官方文档核对到的：
动作名为 QueryHunyuanTo3DRapidJob / SubmitHunyuanTo3DProToJob 系列；
其余常量集中在下面。

与 Meshy/Tripo 的两点不同：

1. **鉴权不是 Bearer**，是腾讯云 TC3-HMAC-SHA256 请求签名：需要 SecretId + SecretKey
   两个值。BYOK 只有一个输入框，所以约定用户填 ``SecretId:SecretKey``（冒号分隔），
   设置页的说明文案也这么写。
2. **查询接口是 POST JSON**，polling.poll_task 只支持 GET 轮询，这里自带一个
   POST 版轮询循环。腾讯的错误约定也特殊：HTTP 200 + Response.Error，必须显式检查。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .base import Gen3DProvider, GenerateRequest, ProviderError, VariantResult
from .polling import DONE_STATES, FAIL_STATES, dig, download

# ---- 端点与动作（改这里；W1 对照官方文档校正） ----
DEFAULT_HOST = "ai3d.tencentcloudapi.com"
SERVICE = "ai3d"
API_VERSION = "2025-01-01"  # ⚠️ W1：以官方文档「X-TC-Version」为准
SUBMIT_ACTION = "SubmitHunyuanTo3DRapidJob"  # ⚠️ W1：极速版提交；专业版换 Pro 系列
QUERY_ACTION = "QueryHunyuanTo3DRapidJob"  # 已见于官方文档 API 概览

# 请求/响应字段（⚠️ W1 对照 API Explorer 校正）
JOB_ID_FIELD = "JobId"
IMAGE_FIELD = "ImageBase64"
PROMPT_FIELD = "Prompt"
OUTPUT_URL_PATHS = (
    "Response.OutputFileUrl",
    "Response.Output.FileUrl",
    "Response.ResultFileUrl",
    "Response.FileUrl",
)
# 腾讯的任务状态枚举与别家不同（疑似 Done/Running/Failed），两边别名都收下
LOCAL_DONE_STATES = DONE_STATES | {"done"}
LOCAL_FAIL_STATES = FAIL_STATES | {"jobfailed"}

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 900.0


def split_credentials(raw: str) -> tuple[str, str]:
    """BYOK 输入框里只有一个值：约定 ``SecretId:SecretKey``。"""
    parts = raw.split(":", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ProviderError(
            "混元3D 的凭据格式是「SecretId:SecretKey」（英文冒号分隔），"
            "在腾讯云 API 密钥管理里可以查到这两个值。"
        )
    return parts[0].strip(), parts[1].strip()


class Hunyuan3DProvider(Gen3DProvider):
    name = "hunyuan3d"
    display_name = "混元3D"
    capabilities = ("image_to_3d", "text_to_3d", "pbr_texture")
    note = "腾讯云混元生3D。凭据填 SecretId:SecretKey（冒号分隔）。端点待 W1 对照官方文档校正。"

    # ---- TC3 签名（算法为腾讯云公共约定，相对可靠；动作名/版本号才是 W1 重点） ----
    def _tc3_headers(self, action: str, payload: str) -> dict[str, str]:
        secret_id, secret_key = split_credentials(self._require_key())
        host = urlparse(self.base_url).netloc or DEFAULT_HOST
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")

        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                f"content-type:application/json; charset=utf-8\nhost:{host}\nx-tc-action:{action.lower()}\n",
                "content-type;host;x-tc-action",
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ]
        )
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                f"{date}/{SERVICE}/tc3_request",
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        def _hmac(key: bytes, message: str) -> bytes:
            return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

        signing_key = _hmac(_hmac(_hmac(f"TC3{secret_key}".encode(), date), SERVICE), "tc3_request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return {
            "X-TC-Action": action,
            "X-TC-Version": API_VERSION,
            "X-TC-Timestamp": str(timestamp),
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={secret_id}/{date}/{SERVICE}/tc3_request, "
                f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
            ),
        }

    async def _post_action(self, client: httpx.AsyncClient, action: str, params: dict) -> dict:
        payload = json.dumps(params, ensure_ascii=False)
        response = await client.post(
            "/", headers=self._tc3_headers(action, payload), content=payload
        )
        if response.status_code >= 400:
            raise ProviderError(
                f"混元3D 调用失败（HTTP {response.status_code}）：{response.text[:200]}"
            )
        body = response.json()
        error = dig(body, "Response.Error")
        if error:
            raise ProviderError(
                f"混元3D 返回错误（{dig(error, 'Code')}）：{dig(error, 'Message')}"
            )
        return body

    async def generate(self, req: GenerateRequest) -> list[VariantResult]:
        images = list(req.image_paths)
        if not images and not req.prompt.strip():
            raise ProviderError("混元3D 需要一张概念图或一段文字描述，两者都没有无法生成。")

        results: list[VariantResult] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as client:
            for index in range(max(1, req.num_variants)):
                image = images[index % len(images)] if images else None
                req.report(index / max(1, req.num_variants) * 0.5, f"提交变体 {index + 1}…")

                submit_params: dict = {}
                if image is not None:
                    submit_params[IMAGE_FIELD] = base64.b64encode(image.read_bytes()).decode("ascii")
                else:
                    submit_params[PROMPT_FIELD] = req.prompt

                created = await self._post_action(client, SUBMIT_ACTION, submit_params)
                job_id = dig(created, f"Response.{JOB_ID_FIELD}")
                if not job_id:
                    raise ProviderError(f"混元3D 未返回任务 ID：{created}")

                state = await self._poll(client, job_id, req, index)
                url = next((u for p in OUTPUT_URL_PATHS if (u := dig(state, p))), "")
                if not url:
                    raise ProviderError("混元3D 任务完成但没有返回模型下载地址。")

                suffix = Path(url.split("?")[0]).suffix or ".glb"
                target = req.out_dir / f"variant_{index + 1:02d}{suffix}"
                await download(client, url, target)

                results.append(
                    VariantResult(
                        provider=self.name,
                        mesh_path=target,
                        params={
                            "job_id": str(job_id),
                            "prompt": req.prompt,
                            "reference_image": str(image) if image else None,
                            "spec": self._spec_dict(req),
                        },
                        raw={"response": dig(state, "Response", {})},
                    )
                )
                req.report(
                    (index + 1) / max(1, req.num_variants) * 0.9,
                    f"已完成 {index + 1}/{req.num_variants} 个变体",
                )
        return results

    async def _poll(
        self, client: httpx.AsyncClient, job_id: str, req: GenerateRequest, index: int
    ) -> dict:
        """腾讯的查询动作是 POST JSON，polling.poll_task 的 GET 轮询用不上。"""
        started = time.monotonic()
        total = max(1, req.num_variants)
        while True:
            state = await self._post_action(client, QUERY_ACTION, {JOB_ID_FIELD: job_id})
            status = str(dig(state, "Response.Status", "") or "").lower()
            if status in LOCAL_DONE_STATES:
                return state
            if status in LOCAL_FAIL_STATES:
                message = (
                    dig(state, "Response.ErrorMessage")
                    or dig(state, "Response.Message")
                    or status
                )
                raise ProviderError(f"混元3D 生成任务失败：{message}")
            if time.monotonic() - started > POLL_TIMEOUT:
                raise ProviderError(f"混元3D 生成超时（已等待 {int(POLL_TIMEOUT)} 秒），请重试。")
            req.report(
                0.5 + (index + 0.5) / total * 0.4,
                f"变体 {index + 1} 生成中（{status or '排队'}）…",
            )
            await asyncio.sleep(POLL_INTERVAL)


__all__ = ["Hunyuan3DProvider", "split_credentials"]

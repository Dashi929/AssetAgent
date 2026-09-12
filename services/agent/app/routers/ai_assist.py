"""AI 助手接口：提示词优化（知识库驱动）与视觉校验（视觉 LLM）。

链路（产品需求 2026-09-12）：
    用户描述/图片 → LLM 按知识库优化提示词 → 生成 → 视觉 LLM 用
    概念图 + 转台渲染图对照校验 → 结构化结论落盘展示。

成本红线：LLM 调用只在用户显式点击时发生（不自动跑），失败给
人话错误；Key 缺失返回 400 而不是 500。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import store, telemetry
from ..ai import llm as llm_module
from ..ai.llm import LLMError
from ..knowledge import prompt_optimization_kb
from ..models import SemanticCheck, SemanticIssue

router = APIRouter(prefix="/api/assets", tags=["ai-assist"])

MAX_CONCEPT_IMAGES = 3
MAX_TURNTABLE_FRAMES = 4


class EnhanceBody(BaseModel):
    description: str = ""


class EnhancePreviewBody(BaseModel):
    description: str
    preset_key: str = ""
    category: str = "prop"
    face_budget: int = 5000
    expected_size_m: float | None = None


def _require_asset(asset_id: str):
    try:
        return store.get_asset(asset_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_llm() -> None:
    if not llm_module.llm_configured():
        raise HTTPException(
            status_code=400,
            detail="还没有配置 AI 助手的 API Key。请到「设置 → AI 助手」填写"
            "（支持智谱 GLM / DeepSeek / 通义等 OpenAI 兼容服务）。",
        )


def _enhance_system(spec_category: str, face_budget: int, expected_size_m, target_engine: str) -> str:
    return (
        "你是游戏资产生成管线的提示词优化助手。以下是本项目的知识库：\n\n"
        f"{prompt_optimization_kb()}\n\n"
        "任务：把用户的口语描述优化成一段可直接用于 3D 生成模型的提示词。\n"
        f"对齐本资产的规格：类目 category={spec_category}，面数预算 {face_budget} 三角面，"
        f"期望尺寸 {expected_size_m or '未指定'}m，目标引擎 {target_engine}。\n"
        '严格输出 JSON：{"prompt": "优化后的完整中文描述", "keywords": ["英文关键词"], '
        '"negative_prompt": "需要避免的要素", "rationale": "一句话说明改了什么"}'
    )


@router.post("/enhance-prompt-preview")
async def enhance_prompt_preview(body: EnhancePreviewBody) -> dict:
    """建资产前的预览优化（不落库）——工作台把结果填回输入框，用户可再编辑。"""
    _require_llm()
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="请先输入一段描述再优化。")
    try:
        result = await llm_module.chat_json(
            _enhance_system(body.category, body.face_budget, body.expected_size_m, "unity"),
            f"用户描述：{body.description.strip()}",
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=502, detail="AI 没有给出有效的优化描述，请重试或换一个模型。")
    telemetry.record("llm_enhance", preview=True, prompt_chars=len(prompt), ok=True)
    return {
        "prompt": prompt,
        "keywords": [str(k).strip() for k in (result.get("keywords") or []) if str(k).strip()],
        "negative_prompt": str(result.get("negative_prompt") or ""),
        "rationale": str(result.get("rationale") or ""),
    }


def _image_sources(asset_id: str) -> list:
    from .assets import _image_sources as _sources

    return _sources(store.get_asset(asset_id))


@router.post("/{asset_id}/enhance-prompt")
async def enhance_prompt(asset_id: str, body: EnhanceBody) -> dict:
    """按知识库优化生成描述：用户口语 → 可直接使用的生成提示词。"""
    asset = _require_asset(asset_id)
    _require_llm()
    description = body.description.strip() or asset.prompt or asset.enhanced_prompt
    if not description:
        raise HTTPException(
            status_code=400,
            detail="没有可优化的描述：请先输入一段文字描述（拖了概念图也可以写一句补充）。",
        )

    spec = asset.spec
    system = _enhance_system(spec.category, spec.face_budget, spec.expected_size_m, spec.target_engine)
    try:
        result = await llm_module.chat_json(system, f"用户描述：{description}")
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    prompt = str(result.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=502, detail="AI 没有给出有效的优化描述，请重试或换一个模型。")

    keywords = [str(k).strip() for k in (result.get("keywords") or []) if str(k).strip()]
    store.patch_asset(asset_id, enhanced_prompt=prompt)
    telemetry.record(
        "llm_enhance",
        asset_id=asset_id,
        keywords=keywords,
        prompt_chars=len(prompt),
        ok=True,
    )
    return {
        "prompt": prompt,
        "keywords": keywords,
        "negative_prompt": str(result.get("negative_prompt") or ""),
        "rationale": str(result.get("rationale") or ""),
    }


@router.post("/{asset_id}/visual-check")
async def visual_check(asset_id: str) -> dict:
    """视觉 LLM 校验：概念图 + 最新转台帧 → 结构化吻合度结论。"""
    asset = _require_asset(asset_id)
    _require_llm()

    head = store.head_version(asset_id)
    if head is None:
        raise HTTPException(status_code=400, detail="这个资产还没有版本，先跑一次管线生成渲染图。")

    images = _collect_check_images(asset_id)
    if not images:
        raise HTTPException(
            status_code=400,
            detail="没有可对照的图片：需要至少一张概念图或一次管线后的转台渲染图。请先跑管线。",
        )

    description = asset.enhanced_prompt or asset.prompt or asset.name
    system = (
        "你是游戏资产的美术质检（QA）。用户会给出参考材料：概念图（如果有）和"
        "3D 生成后处理的转台渲染帧（成品），以及这段资产的文字描述：\n"
        f"{description}\n\n"
        "请从这些维度校验成品：一、形体结构是否与参考/描述一致（主要部件齐全、"
        "比例合理）；二、风格是否匹配；三、有没有明显生成缺陷（碎面、悬空部件、"
        "破损、比例失调、多余物体）；四、作为游戏道具的可用性。\n"
        '严格输出 JSON：{"passed": true/false, "score": 0到100的整数, "summary": "两句话总评", '
        '"issues": [{"severity": "high或medium或low", "message": "问题描述"}], '
        '"suggestions": ["给下一次生成的建议"]}'
    )
    has_concept = bool(_image_sources(asset_id))
    user_text = (
        f"资产名：{asset.name}（类目 {asset.spec.category}）\n"
        f"描述：{description}\n"
        "附图顺序：" + ("概念图在前，" if has_concept else "") + "成品转台帧在后。"
    )
    try:
        result = await llm_module.vision_chat_json(system, user_text, images)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        score = max(0, min(100, int(result.get("score") or 0)))
    except (TypeError, ValueError):
        score = 0
    issues = [
        SemanticIssue(
            severity=str(i.get("severity") or "medium"),
            message=str(i.get("message") or ""),
        )
        for i in (result.get("issues") or [])
        if isinstance(i, dict) and i.get("message")
    ]
    check = store.save_semantic_check(
        SemanticCheck(
            asset_id=asset_id,
            version_id=head.id,
            passed=bool(result.get("passed")),
            score=score,
            summary=str(result.get("summary") or ""),
            issues=issues,
            suggestions=[str(s) for s in (result.get("suggestions") or []) if str(s).strip()],
        )
    )
    telemetry.record(
        "llm_visual_check",
        asset_id=asset_id,
        version_id=head.id,
        passed=check.passed,
        score=check.score,
        images=len(images),
        ok=True,
    )
    return {"check": check.model_dump(mode="json")}


class AiEditBody(BaseModel):
    instruction: str


# LLM 允许修改的资产属性白名单（之外的指令一律拒绝，防止越权改内部字段）
EDITABLE_SPEC = {"face_budget", "expected_size_m", "texture_resolution"}


@router.post("/{asset_id}/ai-edit")
async def ai_edit(asset_id: str, body: AiEditBody) -> dict:
    """预览/编辑模式里的 AI 属性编辑：自然语言指令 → 结构化属性变更 → 应用。

    LLM 只能在白名单内产变更（名称/备注/描述/提示词 + 规格三项），
    应用前后对比返回给前端展示；产出空变更时如实说明而不再猜。
    """
    asset = _require_asset(asset_id)
    _require_llm()
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="请先输入要修改的内容。")

    spec = asset.spec
    context = "\n".join(
        [
            f"资产名：{asset.name}",
            f"素材类型：{asset.kind}",
            f"备注：{asset.notes or '（无）'}",
            f"生成描述：{asset.prompt or asset.enhanced_prompt or '（无）'}",
            f"标签：{'、'.join(asset.tags) or '（无）'}",
            f"规格：面数预算 {spec.face_budget}，期望尺寸 {spec.expected_size_m or '未指定'}m，"
            f"贴图分辨率 {spec.texture_resolution}",
        ]
    )
    system = (
        "你是游戏资产管理器的属性编辑助手。根据用户指令，把要修改的属性以 JSON 变更集返回。\n"
        "只允许修改这些字段：name（资产名，需符合 ^SM_[A-Za-z0-9_]+$）、notes（备注）、"
        "enhanced_prompt（生成描述）、prompt（原始描述）、tags（字符串数组）、"
        "face_budget / expected_size_m / texture_resolution（放在 spec 对象里）。\n"
        "用户没让改的字段**不要出现在变更里**。资产名如果被改，必须符合命名正则。\n"
        '严格输出 JSON：{"changes": {"name": "...", "tags": [...], "spec": {"face_budget": 2000}}, '
        '"summary": "一句话说明改了什么"}'
    )
    try:
        result = await llm_module.chat_json(system, f"当前资产：\n{context}\n\n用户指令：{instruction}")
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    changes = result.get("changes") or {}
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(
            status_code=400,
            detail="AI 没有从指令里解析出可修改的属性。请换一种说法，"
            "例如「把名字改成 SM_Sword_01」「面数预算降到 2000」「标签加上 金属、武器」。",
        )

    before = {
        "name": asset.name,
        "notes": asset.notes,
        "enhanced_prompt": asset.enhanced_prompt,
        "prompt": asset.prompt,
        "tags": list(asset.tags),
        "spec": {
            "face_budget": spec.face_budget,
            "expected_size_m": spec.expected_size_m,
            "texture_resolution": spec.texture_resolution,
        },
    }
    applied: dict[str, Any] = {}
    patch: dict[str, Any] = {}

    if "name" in changes:
        new_name = str(changes["name"]).strip()
        if not re.match(r"^SM_[A-Za-z0-9_]+$", new_name):
            raise HTTPException(status_code=400, detail=f"AI 给出的资产名「{new_name}」不符合命名规范。")
        patch["name"] = new_name
        applied["name"] = new_name
    if "notes" in changes:
        patch["notes"] = str(changes["notes"])
        applied["notes"] = str(changes["notes"])
    if "enhanced_prompt" in changes:
        patch["enhanced_prompt"] = str(changes["enhanced_prompt"])
        applied["enhanced_prompt"] = str(changes["enhanced_prompt"])
    if "prompt" in changes:
        patch["prompt"] = str(changes["prompt"])
        applied["prompt"] = str(changes["prompt"])
    if "tags" in changes and isinstance(changes["tags"], list):
        tags = [str(t).strip() for t in changes["tags"] if str(t).strip()]
        patch["tags"] = tags
        applied["tags"] = tags
    spec_changes = changes.get("spec") or {}
    if isinstance(spec_changes, dict) and spec_changes:
        new_spec = spec.model_copy(update={k: spec_changes[k] for k in EDITABLE_SPEC if k in spec_changes})
        patch["spec"] = new_spec
        applied["spec"] = {k: getattr(new_spec, k) for k in new_spec.model_dump() if k in spec_changes}

    if not applied:
        raise HTTPException(status_code=400, detail="AI 的回复里没有落在白名单内的属性变更。")

    store.patch_asset(asset_id, **patch)
    telemetry.record(
        "llm_edit",
        asset_id=asset_id,
        fields=sorted(applied.keys()),
        instruction_chars=len(instruction),
        ok=True,
    )
    return {"summary": str(result.get("summary") or ""), "applied": applied, "before": before}


def _collect_check_images(asset_id: str) -> list[str]:
    """概念图在前（参考），转台帧在后（成品）。都没有返回空。"""
    images = [str(p) for p in _image_sources(asset_id)][:MAX_CONCEPT_IMAGES]
    turntable = sorted((store.asset_dir(asset_id) / "turntable").glob("turntable_*.png"))
    if turntable:
        step = max(1, len(turntable) // MAX_TURNTABLE_FRAMES)
        images.extend(str(p) for p in turntable[::step][:MAX_TURNTABLE_FRAMES])
    return images

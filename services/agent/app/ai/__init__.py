"""AI 助手子包：LLM 文本/视觉调用与知识库。

设计约定：
- 走 **OpenAI 兼容** 的 chat/completions 协议，一套适配器覆盖
  智谱 GLM / DeepSeek / Qwen(DashScope 兼容模式) / Moonshot / OpenAI 等；
  base_url + model 都可在设置页换。
- Key 复用 BYOK 机制：存 data/secrets.json 的 "llm" 条目，永不明文回显。
- 视觉（VLM）走同一协议的 image_url(base64 data URI) 消息 —— 用来做
  "生成结果 vs 概念图"的语义校验。
"""

from .imagegen import ImageGenError, generate_image
from .llm import (
    LLMError,
    chat,
    chat_json,
    image_content,
    llm_configured,
    vision_chat_json,
)

__all__ = [
    "ImageGenError",
    "LLMError",
    "chat",
    "chat_json",
    "generate_image",
    "image_content",
    "llm_configured",
    "vision_chat_json",
]

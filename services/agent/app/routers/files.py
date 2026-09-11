"""产物文件读取（供前端 3D 视口 / 贴图查看器加载）。

安全约定：**只允许读数据目录内的文件**。这是个本地服务，但仍然不该变成一个
"给个路径就能读任意文件"的接口 —— 一旦用户装了恶意插件或误开端口，那就是个大洞。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..config import get_settings

router = APIRouter(prefix="/api/files", tags=["files"])

MEDIA_TYPES = {
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".obj": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".json": "application/json",
}


@router.get("")
async def get_file(path: str = Query(..., description="数据目录内的绝对路径")) -> FileResponse:
    root = get_settings().data_dir.resolve()
    target = Path(path)

    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="需要绝对路径。")

    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"路径无法解析：{exc}") from exc

    if not resolved.is_relative_to(root):
        raise HTTPException(status_code=403, detail="只允许访问 AssetAgent 数据目录内的文件。")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{resolved.name}")

    return FileResponse(
        resolved,
        media_type=MEDIA_TYPES.get(resolved.suffix.lower(), "application/octet-stream"),
        filename=resolved.name,
    )


__all__ = ["router"]

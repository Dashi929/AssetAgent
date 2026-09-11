"""AssetAgent sidecar 配置。

配置来源优先级：环境变量 / .env > data_dir/settings.json（UI 写入）> 代码默认值。
自填 API Key 只存本机，永不上传、永不入库。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import app_root, default_data_dir, find_recipes_dir

# services/agent/app/config.py -> 仓库根（开发时）；打包后解析为 resources/ 目录
REPO_ROOT = app_root()

RouteMode = Literal["byok", "relay"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path.cwd() / ".env"),
        env_prefix="",
        extra="ignore",
    )

    # ---- 服务 ----
    assetagent_host: str = "127.0.0.1"
    assetagent_port: int = 8756
    assetagent_data_dir: str = ""

    # ---- 路由 ----
    assetagent_route_mode: RouteMode = "byok"
    assetagent_allow_mock_fallback: bool = True

    # ---- BYOK ----
    meshy_api_key: str = ""
    tripo_api_key: str = ""
    rodin_api_key: str = ""
    # 混元3D 凭据约定为 "SecretId:SecretKey"（冒号分隔），见 providers/hunyuan3d.py
    hunyuan3d_api_key: str = ""

    meshy_base_url: str = "https://api.meshy.ai"
    tripo_base_url: str = "https://api.tripo3d.ai"
    # 官方 v2 API 在 hyperhuman.deemos.com（DeemosTech 官方客户端核对，2026-09-12）
    rodin_base_url: str = "https://hyperhuman.deemos.com"
    hunyuan3d_base_url: str = "https://ai3d.tencentcloudapi.com"

    # ---- 成本 ----
    assetagent_cost_per_generation_cny: float = 2.5
    assetagent_monthly_budget_cny: float = 200.0

    # ---- 管线 ----
    assetagent_blender_bin: str = ""
    assetagent_default_face_budget: int = 5000

    # ---- 派生路径 ----
    @property
    def data_dir(self) -> Path:
        raw = self.assetagent_data_dir.strip()
        base = Path(raw).expanduser() if raw else default_data_dir()
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def assets_dir(self) -> Path:
        p = self.data_dir / "assets"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def trash_dir(self) -> Path:
        """归档区。任何"删除"都只是移动到这里，绝不真删。"""
        p = self.data_dir / "archive"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def secrets_file(self) -> Path:
        """UI 写入的自填 Key。仅本机，权限收紧，已 gitignore。"""
        return self.data_dir / "secrets.json"

    @property
    def usage_file(self) -> Path:
        return self.data_dir / "usage.jsonl"

    @property
    def telemetry_file(self) -> Path:
        """本地埋点 JSONL（见 app/telemetry.py）。默认不上传。"""
        return self.data_dir / "telemetry.jsonl"

    @property
    def recipes_dir(self) -> Path:
        """校验规则与预设目录。打包后从 resources/recipes 读取。"""
        return find_recipes_dir()

    # ---- BYOK ----
    def provider_key(self, provider: str) -> str:
        """Key 查找顺序：settings.json（UI 填写）> 环境变量。"""
        name = provider.strip().lower()
        stored = self._stored_secrets().get(name)
        if stored:
            return str(stored).strip()
        return str(getattr(self, f"{name}_api_key", "") or "").strip()

    def set_provider_key(self, provider: str, key: str) -> None:
        secrets = self._stored_secrets()
        name = provider.strip().lower()
        if key:
            secrets[name] = key
        else:
            secrets.pop(name, None)
        self._write_json_private(self.secrets_file, secrets)

    def _stored_secrets(self) -> dict[str, Any]:
        if not self.secrets_file.exists():
            return {}
        try:
            return json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        # 尽力收紧权限；Windows 上 chmod 语义有限，失败不影响正确性
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    # ---- UI 可写的非敏感设置 ----
    def runtime_overrides(self) -> dict[str, Any]:
        if not self.settings_file.exists():
            return {}
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def patch_runtime(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.runtime_overrides()
        current.update({k: v for k, v in patch.items() if v is not None})
        self._write_json_private(self.settings_file, current)
        return current

    def effective(self, key: str, default: Any = None) -> Any:
        """读配置的统一入口：运行时覆盖（UI 写入）> 环境变量 > 默认值。

        key 传不带前缀的名字，例如 effective("route_mode")、effective("monthly_budget_cny")。
        """
        value = self.runtime_overrides().get(key)
        if value is not None:
            return value
        return getattr(self, f"assetagent_{key}", default)

    # ---- Blender 探测 ----
    def blender_bin(self) -> str | None:
        """优先用显式配置，其次 PATH，最后扫常见安装位置。找不到返回 None（管线会降级）。"""
        configured = str(self.effective("blender_bin", "") or "").strip()
        if configured and Path(configured).exists():
            return configured
        found = shutil.which("blender")
        if found:
            return found

        candidates: list[Path] = []
        for pattern in (
            r"C:\Program Files\Blender Foundation\Blender*\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender*\blender-launcher.exe",
            "/Applications/Blender.app/Contents/MacOS/Blender",
            "/usr/bin/blender",
        ):
            path = Path(pattern)
            if "*" in pattern:
                # 多个版本时取版本号最大的那个
                candidates.extend(sorted(path.parent.glob(path.name), reverse=True))
            elif path.exists():
                candidates.append(path)
        return str(candidates[0]) if candidates else None

    # ---- 用量台账 ----
    def record_usage(self, entry: dict[str, Any]) -> None:
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def usage_summary(self, month: str | None = None) -> dict[str, Any]:
        """month 形如 2026-09；不传则统计全部。"""
        total = 0.0
        count = 0
        if self.usage_file.exists():
            for line in self.usage_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if month and not str(entry.get("at", "")).startswith(month):
                    continue
                total += float(entry.get("cost", 0) or 0)
                count += 1
        return {"month": month, "generations": count, "cost": round(total, 4)}

    def budget_exceeded(self, additional: float = 0.0) -> bool:
        limit = float(self.effective("monthly_budget_cny", 200.0) or 0)
        if limit <= 0:
            return False
        import datetime as _dt

        spent = self.usage_summary(month=_dt.datetime.now().strftime("%Y-%m"))["cost"]
        return (spent + additional) > limit

    @property
    def cost_per_generation(self) -> float:
        return float(self.effective("cost_per_generation_cny", 2.5) or 0.0)

    @property
    def default_face_budget(self) -> int:
        return int(self.effective("default_face_budget", 5000) or 5000)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = ["REPO_ROOT", "Settings", "get_settings", "reset_settings_cache"]

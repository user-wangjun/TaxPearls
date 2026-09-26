"""Server-only configuration. Environment variables override the project .env."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def flag(name, default=False):
    value = os.getenv(name, "1" if default else "0").strip().lower()
    if value not in {"1", "0", "true", "false"}:
        raise ValueError(f"{name} 须为 1/0 或 true/false")
    return value in {"1", "true"}


def integer(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 须为整数") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 须在 {minimum}–{maximum} 之间")
    return value


@dataclass(frozen=True)
class AISettings:
    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    model: str = "ds-v4.1f"
    vision: bool = True
    json_mode: bool = True
    disable_thinking: bool = True
    timeout: int = 90
    batch_timeout: int = 300
    max_tokens: int = 8192
    max_pages: int = 12
    max_calls: int = 20

    @classmethod
    def from_env(cls):
        return cls(enabled=flag("TAXPEARLS_AI_ENABLED"),
                   base_url=os.getenv("TAXPEARLS_AI_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
                   api_key=os.getenv("TAXPEARLS_AI_API_KEY", "").strip(),
                   model=os.getenv("TAXPEARLS_AI_MODEL", "ds-v4.1f").strip(),
                   vision=flag("TAXPEARLS_AI_VISION", True),
                   json_mode=flag("TAXPEARLS_AI_JSON_MODE", True),
                   disable_thinking=flag("TAXPEARLS_AI_DISABLE_THINKING", True),
                   timeout=integer("TAXPEARLS_AI_TIMEOUT", 90, 5, 180),
                   batch_timeout=integer("TAXPEARLS_AI_BATCH_TIMEOUT", 300, 30, 600),
                   max_tokens=integer("TAXPEARLS_AI_MAX_TOKENS", 8192, 1024, 32768),
                   max_pages=integer("TAXPEARLS_AI_MAX_PAGES", 12, 1, 50),
                   max_calls=integer("TAXPEARLS_AI_MAX_CALLS", 20, 1, 100))

    @property
    def effective_model(self):
        if urlsplit(self.base_url).hostname == "api.deepseek.com" and self.model == "ds-v4.1f":
            return "deepseek-flash"
        return self.model

    def problem(self):
        if not self.enabled:
            return "AI 提取未启用；当前使用本地解析。"
        if not self.api_key or not self.base_url or not self.model:
            return "AI 未配置完整：请填写 .env 中的 API_KEY、BASE_URL 和 MODEL 后重启服务。"
        try:
            parsed = urlsplit(self.base_url)
            _ = parsed.port
        except ValueError:
            return "AI 接口地址格式错误。"
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            return "AI 接口地址须为不含认证信息或查询参数的基础地址。"
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            return "AI 接口须使用 HTTPS（本机接口可使用 HTTP）。"
        if parsed.path.rstrip("/").endswith("chat/completions"):
            return "BASE_URL 填基础地址即可，不要包含 /chat/completions。"
        return ""

    def public_status(self):
        problem = self.problem()
        return {"enabled": self.enabled, "ready": not problem, "model": self.model,
                "effective_model": self.effective_model, "vision": self.vision,
                "message": problem or f"AI 已配置：{self.model}；所选材料的文字及页面图片会发送给配置的模型服务。"}

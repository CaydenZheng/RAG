"""
Prompt 管理器 — YAML 加载、版本切换、模板渲染。

用法:
    from src.infra.prompt_manager import prompt_manager
    rendered = prompt_manager.render("answer_generation", query="...", context="...")
"""

import yaml
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger

from config.settings import settings


class PromptManager:
    """加载 prompts/{version}/*.yaml，支持模板渲染"""

    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._version = settings.prompt_version

    @property
    def prompt_dir(self) -> Path:
        return Path("prompts") / self._version

    def load(self, name: str) -> dict:
        """加载一个 prompt 模板（带缓存）"""
        if name not in self._cache:
            path = self.prompt_dir / f"{name}.yaml"
            if not path.exists():
                raise FileNotFoundError(f"Prompt template not found: {path}")
            with open(path, "r", encoding="utf-8") as f:
                self._cache[name] = yaml.safe_load(f)
            logger.debug("Loaded prompt: {} (v{})", name, self._cache[name].get("version"))
        return self._cache[name]

    def render(self, name: str, **kwargs) -> Dict[str, Any]:
        """
        加载并渲染 prompt 模板。

        Returns:
            dict 包含 system, user (渲染后的), model, temperature 等
        """
        template = self.load(name)

        system = template.get("system", "")
        user_template = template.get("user_template", "")

        # 渲染 user 模板
        user = user_template.format(**kwargs) if kwargs else user_template

        return {
            "system": system.strip(),
            "user": user.strip(),
            "model": template.get("model", settings.llm_model),
            "temperature": template.get("temperature", 0.3),
            "max_tokens": template.get("max_tokens"),
        }

    def render_chat_messages(self, name: str, **kwargs) -> list[dict]:
        """
        渲染为 OpenAI 兼容的 messages 列表。

        Returns:
            [{"role": "system", "content": ...}, {"role": "user", "content": ...}]
        """
        rendered = self.render(name, **kwargs)
        return [
            {"role": "system", "content": rendered["system"]},
            {"role": "user", "content": rendered["user"]},
        ]

    def get_prompt_config(self, name: str) -> dict:
        """
        获取 prompt 模板的完整配置（不含渲染）。

        Returns:
            {"model": ..., "temperature": ..., "max_tokens": ...}
        """
        template = self.load(name)
        return {
            "model": template.get("model", settings.llm_model),
            "temperature": template.get("temperature", 0.3),
            "max_tokens": template.get("max_tokens", 1024),
        }


# 全局单例
prompt_manager = PromptManager()

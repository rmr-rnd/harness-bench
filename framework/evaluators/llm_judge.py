"""
LLM-as-judge client — thin wrapper over OpenAI-compatible API.
Benchmark-specific grading logic lives inside each benchmark
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openai import OpenAI

if TYPE_CHECKING:
    from framework.config import ModelConfig


class LLMJudge:
    def __init__(self, cfg: "ModelConfig") -> None:
        self.cfg = cfg
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "sk-none")

    def _call(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = self._client.chat.completions.create(
            model=self.cfg.model_name,
            messages=messages,
            temperature=0.0,
            max_tokens=16384,
        )
        content = resp.choices[0].message.content
        if not content:
            # Some models put output in reasoning_content when thinking is enabled
            content = getattr(resp.choices[0].message, "reasoning_content", None) or ""
        return content.strip()

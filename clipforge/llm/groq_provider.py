"""Groq free tier (OpenAI-compatible API; needs GROQ_API_KEY from console.groq.com)."""

from __future__ import annotations

import time

import httpx

from clipforge.llm.base import LLMError, LLMProvider

API = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str | None, model: str = "auto", temperature: float = 0.3, timeout: int = 300) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys and add it to .env")
        self.api_key = api_key
        super().__init__(DEFAULT_MODEL if model == "auto" else model, temperature, timeout)

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "",
                 json_schema: dict | None = None, options: dict | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(4):
            r = httpx.post(API, json=body, timeout=self.timeout, headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", 15 * (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 400:
                raise LLMError(f"Groq error {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]["content"]
        raise LLMError("Groq free-tier rate limit hit repeatedly. Wait a minute or use --provider ollama.")

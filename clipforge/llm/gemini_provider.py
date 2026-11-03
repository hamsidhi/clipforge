"""Google Gemini via the free-tier REST API (needs GEMINI_API_KEY; free key at aistudio.google.com)."""

from __future__ import annotations

import time

import httpx

from clipforge.llm.base import LLMError, LLMProvider

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None, model: str = "auto", temperature: float = 0.3, timeout: int = 300) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set. Get a free key at https://aistudio.google.com/apikey and add it to .env")
        self.api_key = api_key
        super().__init__(DEFAULT_MODEL if model == "auto" else model, temperature, timeout)

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "",
                 json_schema: dict | None = None) -> str:
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": self.temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        for attempt in range(4):
            r = httpx.post(API.format(model=self.model), json=body, timeout=self.timeout,
                           headers={"x-goog-api-key": self.api_key})
            if r.status_code == 429:  # free-tier rate limit: back off and retry
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"Gemini error {r.status_code}: {r.text[:300]}")
            data = r.json()
            try:
                return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError) as exc:
                raise LLMError(f"Gemini returned no text (blocked or empty): {str(data)[:300]}") from exc
        raise LLMError("Gemini free-tier rate limit hit repeatedly. Wait a minute or use --provider ollama.")

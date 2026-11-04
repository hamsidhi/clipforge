"""LLM provider interface + JSON-with-retry helper shared by every provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from clipforge.utils.log import log
from clipforge.utils.text import extract_json

T = TypeVar("T", bound=BaseModel)


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON schema with EVERY property required (pydantic marks fields with defaults optional, and a
    schema-constrained model then happily omits them — e.g. all scores missing)."""
    schema = model.model_json_schema()

    def fix(node: dict) -> None:
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
        for v in node.get("$defs", {}).values():
            fix(v)

    fix(schema)
    return schema


class LLMError(RuntimeError):
    """The provider could not be reached or kept returning invalid output."""


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, model: str, temperature: float = 0.3, timeout: int = 600) -> None:
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "",
                 json_schema: dict | None = None, options: dict | None = None) -> str:
        """Return the raw text completion. `task` is a hint used only by the mock provider; `json_schema`
        constrains the output on providers that support structured outputs (Ollama); `options` are per-call
        Ollama options (num_predict, num_ctx, …) that override the provider defaults — other providers ignore them."""

    def complete_json(
        self, prompt: str, schema: type[T], *, system: str | None = None, retries: int = 3, task: str = "",
        json_schema: dict | None = None, options: dict | None = None,
    ) -> T:
        """Ask for JSON matching `schema`; on parse/validation errors, retry with the error appended."""
        last_err = ""
        attempt_prompt = prompt
        for attempt in range(1, retries + 1):
            try:
                raw = self.generate(attempt_prompt, system=system, json_mode=True, task=task, json_schema=json_schema,
                                    options=options)
            except LLMError:
                raise
            except Exception as exc:  # network etc.
                raise LLMError(f"{self.name} request failed: {exc}") from exc
            try:
                data = extract_json(raw)
                return schema.model_validate(data)
            except (ValueError, ValidationError) as exc:
                last_err = str(exc)[:800]
                log.warning(f"{self.name}: invalid JSON (attempt {attempt}/{retries}): {last_err.splitlines()[0]}")
                attempt_prompt = (
                    f"{prompt}\n\nYour previous reply was invalid:\n{last_err}\n"
                    "Reply again with ONLY valid JSON matching the required format."
                )
        raise LLMError(f"{self.name} returned invalid JSON {retries} times. Last error: {last_err}")

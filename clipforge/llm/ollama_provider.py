"""Local LLM via Ollama (default — works fully offline, no API key)."""

from __future__ import annotations

import httpx

from clipforge.llm.base import LLMError, LLMProvider

# Preferred general-purpose models, best first. Coder models are a last resort (weaker at Hinglish).
PREFERRED = ["qwen3", "qwen2.5:14b", "qwen2.5:7b", "qwen2.5", "llama3.1", "llama3.2", "gemma3", "gemma2", "mistral", "phi4"]


def list_ollama_models(url: str = "http://127.0.0.1:11434") -> list[str]:
    r = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def pick_model(installed: list[str]) -> str | None:
    """Choose the best installed model for clip selection."""
    general = [m for m in installed if "embed" not in m and "coder" not in m]
    for pref in PREFERRED:
        for m in general:
            if m.startswith(pref):
                return m
    if general:
        return general[0]
    return installed[0] if installed else None


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "auto", temperature: float = 0.3, timeout: int = 600,
                 url: str = "http://127.0.0.1:11434", max_tokens: int = 2048, keep_alive: str = "30m",
                 num_thread: int = 0, seed: int | None = None) -> None:
        self.url = url.rstrip("/")
        self.keep_alive = keep_alive
        self.num_thread = num_thread  # 0 = Ollama's default (one thread per physical core)
        self.seed = seed  # fixed seed = reproducible answers for the same prompt (benchmarks, re-runs)
        if model == "auto":
            try:
                installed = list_ollama_models(self.url)
            except Exception as exc:
                raise LLMError(
                    f"Ollama is not running at {self.url}. Start the Ollama app (or `ollama serve`), "
                    "or choose another provider (--provider gemini/groq/mock)."
                ) from exc
            chosen = pick_model(installed)
            if not chosen:
                raise LLMError("Ollama has no models. Run: ollama pull qwen2.5:7b")
            model = chosen
        super().__init__(model, temperature, timeout)
        self.max_tokens = max_tokens
        self.calls: list[dict] = []  # Ollama's own counters per request (prompt/eval tokens + seconds) for benchmarks

    def generate(self, prompt: str, *, system: str | None = None, json_mode: bool = True, task: str = "",
                 json_schema: dict | None = None, options: dict | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # num_predict caps runaway generations (JSON mode can otherwise emit whitespace until the context
            # is full — minutes on a CPU); keep_alive avoids reloading the model between windows
            "options": {"temperature": self.temperature, "num_ctx": 16384, "num_predict": self.max_tokens,
                        **({"num_thread": self.num_thread} if self.num_thread else {}),
                        **({"seed": self.seed} if self.seed is not None else {}), **(options or {})},
            "keep_alive": self.keep_alive,
        }
        if json_mode:
            body["format"] = json_schema or "json"  # a schema = Ollama structured outputs
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Ollama at {self.url}: {exc}") from exc
        if r.status_code == 404:
            raise LLMError(f"Ollama model '{self.model}' not found. Run: ollama pull {self.model}")
        if r.status_code >= 400:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.calls.append(_stats(data, task))
        return data["message"]["content"]


def _stats(data: dict, task: str) -> dict:
    ns = 1e9
    return {
        "task": task,
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
        "prompt_sec": round(data.get("prompt_eval_duration", 0) / ns, 2),
        "output_sec": round(data.get("eval_duration", 0) / ns, 2),
        "load_sec": round(data.get("load_duration", 0) / ns, 2),
        "total_sec": round(data.get("total_duration", 0) / ns, 2),
    }

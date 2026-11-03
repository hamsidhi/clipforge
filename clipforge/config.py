"""Configuration: `config.yaml` (behaviour) + `.env` (secrets).

Every section is a pydantic model so a typo in config.yaml fails loudly with a
clear message instead of being silently ignored.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"
ENV_PATH = ROOT_DIR / ".env"
ASSETS_DIR = ROOT_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
MODELS_DIR = ASSETS_DIR / "models"
PROMPTS_DIR = ROOT_DIR / "prompts"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ClipsConfig(_Section):
    count: int = Field(20, ge=1, le=50)
    min_count: int = 15
    max_count: int = 25
    min_sec: float = Field(20, gt=0)
    max_sec: float = Field(59, gt=0)
    max_overlap: float = Field(0.3, ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> "ClipsConfig":
        if self.min_sec >= self.max_sec:
            raise ValueError("clips.min_sec must be smaller than clips.max_sec")
        return self


class WhisperConfig(_Section):
    model: str = "auto"
    language: str = "hi"
    compute_type: str = "auto"
    vad: bool = True
    beam_size: int = 5


class LLMConfig(_Section):
    provider: Literal["ollama", "gemini", "groq", "mock"] = "ollama"
    model: str = "auto"
    temperature: float = 0.3
    window_min: float = 8
    overlap_min: float = 1
    max_retries: int = 3
    max_candidates_per_window: int = 6
    ollama_url: str = "http://localhost:11434"
    timeout_sec: int = 600


class ScoringWeights(_Section):
    hook: float = 0.3
    standalone: float = 0.25
    emotion: float = 0.15
    value: float = 0.2
    controversy: float = 0.1


class ReframeConfig(_Section):
    layout: Literal["auto", "track", "split", "blur"] = "auto"
    sample_fps: float = 5
    smoothing: float = Field(0.85, ge=0, lt=1)
    deadzone: float = 0.04
    split_min_distance: float = 0.35


class CaptionsConfig(_Section):
    script: Literal["roman", "devanagari"] = "roman"
    preset: str = "bold-yellow-pop"
    words_per_line: int = Field(3, ge=1, le=6)
    max_chars_per_line: int = 18
    font: str = "Montserrat-ExtraBold"
    font_size: int = 82
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    keyword_color: str = "#00FF88"
    outline: int = 6
    shadow: int = 3
    position_pct: float = Field(0.65, ge=0.3, le=0.8)
    hook_title: bool = True
    hook_seconds: float = 3


class RenderConfig(_Section):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "veryfast"
    loudness_lufs: float = -14
    audio_bitrate: str = "192k"
    use_nvenc: Literal["auto", True, False] | str = "auto"
    workers: int | Literal["auto"] = "auto"


class OutputConfig(_Section):
    dir: str = "workspace"


class UploadConfig(_Section):
    enabled: bool = False
    privacy: Literal["private", "unlisted", "public"] = "private"
    schedule_hours_apart: float = 24
    category_id: str = "22"


class Config(_Section):
    clips: ClipsConfig = ClipsConfig()
    whisper: WhisperConfig = WhisperConfig()
    llm: LLMConfig = LLMConfig()
    scoring_weights: ScoringWeights = ScoringWeights()
    reframe: ReframeConfig = ReframeConfig()
    captions: CaptionsConfig = CaptionsConfig()
    render: RenderConfig = RenderConfig()
    output: OutputConfig = OutputConfig()
    upload: UploadConfig = UploadConfig()

    @property
    def workspace(self) -> Path:
        """Absolute workspace directory (relative paths resolve against the repo root)."""
        p = Path(self.output.dir).expanduser()
        return p if p.is_absolute() else ROOT_DIR / p

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        """Return a copy with dotted-key overrides applied, e.g. {"clips.count": 15}."""
        data = self.model_dump()
        for key, value in overrides.items():
            if value is None:
                continue
            node = data
            parts = key.split(".")
            for part in parts[:-1]:
                node = node[part]
            node[parts[-1]] = value
        return Config.model_validate(data)


class Secrets(BaseSettings):
    """API keys from the environment or `.env` (both optional)."""

    model_config = SettingsConfigDict(env_file=str(ENV_PATH), env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str | None = None
    groq_api_key: str | None = None


class ConfigError(RuntimeError):
    """Raised when config.yaml is missing or invalid."""


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate config.yaml. Missing file -> defaults."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path} has invalid values:\n{exc}") from exc


def save_config(cfg: Config, path: Path | str | None = None) -> None:
    """Write the config back to YAML (used by the Settings page)."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_secrets() -> Secrets:
    return Secrets()


def save_secret(name: str, value: str) -> None:
    """Upsert KEY=value in .env and the current process environment."""
    name = name.upper()
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in lines if not ln.startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    ENV_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[:4]}…{value[-2:]}" if len(value) > 8 else "••••"

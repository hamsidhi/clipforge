"""Configuration: `config.yaml` (behaviour) + `.env` (secrets).

Every section is a pydantic model so a typo in config.yaml fails loudly with a
clear message instead of being silently ignored.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"
ENV_PATH = Path(os.environ["CLIPFORGE_ENV"]) if os.environ.get("CLIPFORGE_ENV") else ROOT_DIR / ".env"  # override: tests / a 2nd instance
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
    beam_size: int | Literal["auto"] = "auto"  # auto -> 5 on GPU, 1 (greedy, ~2x faster) on CPU


class TranscriptConfig(_Section):
    source: Literal["auto", "pasted", "youtube", "whisper"] = "auto"  # auto: pasted > YouTube captions > Whisper
    youtube_languages: list[str] = ["hi", "en"]  # caption languages to try first (then any)
    clip_pad_sec: float = 1.5  # estimated timing: Whisper runs only on each chosen clip ± this
    max_duration_gap: float = 0.10  # warn when a pasted transcript's end is this far (fraction) from the video's


class LLMConfig(_Section):
    provider: Literal["ollama", "gemini", "groq", "mock"] = "ollama"
    model: str = "auto"
    temperature: float = 0.2
    window_min: float = 8
    overlap_min: float = 1
    max_retries: int = 3
    max_candidates_per_window: int = 6
    ollama_url: str = "http://127.0.0.1:11434"
    timeout_sec: int = 600
    max_tokens: int = 2048  # cap per LLM reply (6 candidates ≈ 900 tokens)
    keep_alive: str = "30m"  # Ollama: unload the model this long after the last request (server: "2m" frees RAM for Whisper)
    num_thread: int = Field(0, ge=0)  # Ollama CPU threads; 0 = physical cores
    seed: int | None = 42  # Ollama sampling seed: same prompt -> same answer (None = random)


class SelectConfig(_Section):
    prefilter: bool = True  # skip transcript windows that are clearly intro / sponsor read / outro / almost no speech
    rerank: bool = False    # final pass: the LLM re-scores the best candidates with surrounding context
    rerank_pool: int = Field(40, ge=5, le=100)  # how many top candidates the re-rank pass looks at
    rerank_context_sec: float = Field(20, ge=0, le=60)  # context shown before/after each candidate


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


class PublishingConfig(_Section):
    """YouTube publishing defaults. Per-channel settings (slots, templates, …) live in the Channels page/DB."""

    mode: Literal["assisted", "api"] = "assisted"
    api_audited: bool = False
    unverified_lock: bool = False  # Google lifted the private-only lock for unverified projects on 2026-11-02
    timezone: str = "Asia/Kolkata"
    slots: list[str] = ["12:30", "18:30", "21:00"]
    peak_slots: list[str] = ["18:30", "21:00"]
    posts_per_day: int = Field(3, ge=1, le=20)
    min_gap_hours: float = 2.5  # default slots 18:30 and 21:00 are 2.5 h apart
    upload_ahead_days: int = Field(0, ge=0, le=60)  # 0 = upload as soon as a clip is scheduled
    late_policy: Literal["next_slot", "publish_now", "ask"] = "next_slot"  # api mode: slot <15 min away or passed
    audit_status: Literal["not_applied", "applied", "approved", "rejected"] = "not_applied"
    audit_applied_on: str = ""
    insert_calls_per_day: int = 100   # videos.insert has its own bucket (Data API, since 2026-06-01)
    units_per_day: int = 10_000       # all other Data API calls
    batch_max: int = 15
    category_id: str = "27"
    language: str = "hi"
    sync_every_hours: float = 12


class YppConfig(_Section):
    """YouTube Partner Program targets (edit when YouTube changes them)."""

    ad_subs: int = 1000
    ad_shorts_views_90d: int = 10_000_000
    fan_subs: int = 500
    fan_shorts_views_90d: int = 3_000_000
    fan_uploads_90d: int = 3
    new_rule_date: str = "2027-02-01"
    new_ad_shorts_views_90d: int = 20_000_000


class InstagramConfig(_Section):
    """Instagram Reels publishing (Phase 13). Per-account slots live on the Accounts page."""

    mode: Literal["api", "assisted"] = "api"   # assisted = an "Instagram kit" you post yourself
    graph_version: str = "v26.0"
    slots: list[str] = ["13:00", "19:00", "21:30"]
    peak_slots: list[str] = ["19:00", "21:30"]
    posts_per_day: int = Field(3, ge=1, le=20)
    min_gap_hours: float = 2.0
    upload_lead_minutes: int = Field(20, ge=5, le=600)  # create + upload the container this early, publish at the slot
    max_late_minutes: int = Field(90, ge=0, le=1440)    # later than this → "missed", never published late
    publish_limit: int = 100            # API-published posts per moving 24 h (Meta docs, checked 2026-11-04)
    publish_limit_margin: int = 10      # stay this far below the limit
    hours_after_youtube: float = 0      # "post to Instagram X hours after YouTube" (0 = same day, its own slot)
    share_to_feed: bool = True


class AuthConfig(_Section):
    """Login for remote use (Phase 14). Localhost on the laptop can stay passwordless."""

    trust_localhost: bool = True
    session_days: int = Field(30, ge=1, le=365)
    max_failures: int = Field(5, ge=3, le=20)     # wrong passwords before a lockout
    lockout_minutes: int = Field(15, ge=1, le=1440)


class PowerConfig(_Section):
    keep_awake: Literal["always", "when_busy", "never"] = "when_busy"


class TelegramConfig(_Section):
    """Telegram bot (long polling from the laptop — no public server). Token lives in .env (TELEGRAM_BOT_TOKEN)."""

    enabled: bool = False
    notify_job_done: bool = True
    notify_job_failed: bool = True
    notify_published: bool = True
    notify_publish_failed: bool = True
    notify_reauth: bool = True
    notify_quota: bool = True
    daily_summary: bool = True
    daily_summary_time: str = "22:00"   # IST
    # Phase 15: full remote control
    api_base_url: str = "https://api.telegram.org"   # self-hosted Bot API server (2 GB files), e.g. http://127.0.0.1:8081
    clips_to_send: int = Field(5, ge=0, le=25)       # clip cards sent when a job finishes (album + one card each)
    progress_every_sec: int = Field(8, ge=5, le=60)  # live progress message: edit at most this often
    pin_required: bool = False          # ask the PIN (hash in .env TELEGRAM_PIN_HASH) for publish now / public / delete / unpause
    commands_per_minute: int = Field(30, ge=5, le=300)  # per chat; more are ignored
    mini_app: bool = False              # "Open app" buttons as a Telegram Mini App (needs Tailscale HTTPS; see docs/TELEGRAM.md)

    @model_validator(mode="after")
    def _safe_api_base(self) -> "TelegramConfig":
        """The bot token travels in every request URL: only Telegram itself or a server on this machine."""
        import re as _re

        u = self.api_base_url.rstrip("/")
        if u != "https://api.telegram.org" and not _re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):\d{2,5}", u):
            raise ValueError("telegram.api_base_url must be https://api.telegram.org or a local server like http://127.0.0.1:8081")
        return self


class RemoteConfig(_Section):
    watch_folder: str = ""              # `clipforge watch`: auto-start jobs for files dropped here (off when empty)
    preview_proxy: bool = True          # also render a 540×960 ~1 Mbps preview per clip (phones / data saver)


class DeploymentConfig(_Section):
    """Phase 15b: where things run. standalone = everything on this computer (default, as before).
    server = the always-on machine: owns the database, tokens, publishing and the Telegram poller.
    worker = a laptop that takes heavy jobs from the server and never publishes or polls Telegram."""

    mode: Literal["standalone", "server", "worker"] = "standalone"
    node_name: str = ""                 # shown in Telegram ("on laptop"); empty = computer name
    server_url: str = ""                # worker: the server over Tailscale, e.g. http://clipforge-server:8000
    capabilities: list[str] = ["download", "transcribe", "select", "render"]  # worker: stages it accepts
    max_parallel: int = Field(1, ge=1, le=4)
    prefer_worker_for: list[str] = ["download", "transcribe", "select", "render"]  # server: wait for a worker for these
    worker_grace_minutes: int = Field(10, ge=0, le=1440)  # server: how long a stage waits for a worker before doing it itself
    lease_seconds: int = Field(120, ge=30, le=3600)       # a claimed task is re-queued if not renewed in time
    heartbeat_seconds: int = Field(30, ge=5, le=600)


class ServerConfig(_Section):
    """Phase 15b housekeeping + backups (server or standalone)."""

    keep_sources_days: int = Field(14, ge=1)          # delete source video/audio of finished jobs after N days
    keep_clips_days_after_publish: int = Field(14, ge=0)  # delete rendered clips N days after all their posts went out
    disk_warn_pct: int = Field(80, ge=50, le=99)
    disk_pause_pct: int = Field(90, ge=50, le=99)      # new jobs are refused above this
    backup_dir: str = ""                # where nightly backups go ('' = workspace/backups)
    backup_recipient: str = ""          # age public key (age1…) — backups are encrypted to it; the private key stays with you
    backup_keep_daily: int = Field(7, ge=1)
    backup_keep_weekly: int = Field(4, ge=0)
    backup_time: str = "03:30"          # IST
    rclone_remote: str = ""             # optional copy to e.g. gdrive-crypt:clipforge (an rclone crypt remote)
    download_cookies_file: str = ""     # optional yt-dlp cookies.txt (server only; use a throwaway Google account)
    download_max_attempts: int = Field(3, ge=1, le=6)  # bot-check retries with backoff before asking you

    @model_validator(mode="after")
    def _valid_recipient(self) -> "ServerConfig":
        r = self.backup_recipient.strip()
        if r and not (r.startswith("age1") and len(r) > 50):
            import base64 as _b64

            try:
                ok = r.startswith("cfpk1:") and len(_b64.urlsafe_b64decode(r[6:])) == 32
            except Exception:
                ok = False
            if not ok:
                raise ValueError("server.backup_recipient must be a cfpk1:… key (clipforge server backup-keygen) or an age1… key")
        return self


class Config(_Section):
    clips: ClipsConfig = ClipsConfig()
    whisper: WhisperConfig = WhisperConfig()
    transcript: TranscriptConfig = TranscriptConfig()
    llm: LLMConfig = LLMConfig()
    select: SelectConfig = SelectConfig()
    scoring_weights: ScoringWeights = ScoringWeights()
    reframe: ReframeConfig = ReframeConfig()
    captions: CaptionsConfig = CaptionsConfig()
    render: RenderConfig = RenderConfig()
    output: OutputConfig = OutputConfig()
    publishing: PublishingConfig = PublishingConfig()
    ypp: YppConfig = YppConfig()
    instagram: InstagramConfig = InstagramConfig()
    auth: AuthConfig = AuthConfig()
    power: PowerConfig = PowerConfig()
    telegram: TelegramConfig = TelegramConfig()
    remote: RemoteConfig = RemoteConfig()
    deployment: DeploymentConfig = DeploymentConfig()
    server: ServerConfig = ServerConfig()

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
    meta_app_id: str | None = None          # Instagram (Phase 13): Meta app id + secret
    meta_app_secret: str | None = None
    meta_login_config_id: str | None = None   # optional "Facebook Login for Business" configuration id
    telegram_bot_token: str | None = None   # Phase 14
    telegram_chat_id: str | None = None     # paired Telegram user id (only this user may talk to the bot)
    clipforge_password_hash: str | None = None  # Phase 14: login password (scrypt hash, never the password)
    telegram_pin_hash: str | None = None    # Phase 15: PIN for dangerous Telegram actions (scrypt hash)
    clipforge_worker_key: str | None = None  # Phase 15b, worker side: this laptop's key for the server


class ConfigError(RuntimeError):
    """Raised when config.yaml is missing or invalid."""


def config_path() -> Path:
    """config.yaml in the repo, or the file named by CLIPFORGE_CONFIG (e.g. a demo/test workspace)."""
    env = os.environ.get("CLIPFORGE_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate config.yaml. Missing file -> defaults."""
    path = Path(path) if path else config_path()
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


_KEY_LINE = re.compile(
    r"""^(?P<indent>\s+)(?P<key>[A-Za-z_][\w]*):(?P<sp>\s*)(?P<value>"[^"]*"|'[^']*'|[^#\n]*?)(?P<comment>\s+#.*)?$"""
)
_SECTION_LINE = re.compile(r"^(?P<key>[A-Za-z_][\w]*):\s*(#.*)?$")


def _yaml_scalar(v: Any) -> str:
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True).strip().removesuffix("\n...").strip()


def update_yaml_text(text: str, data: dict[str, dict[str, Any]]) -> tuple[str, bool]:
    """Change values in a two-level YAML file in place, keeping comments and layout.

    Returns (new_text, complete) — complete is False if some key wasn't found in the text."""
    remaining = {(s, k) for s, vals in data.items() for k in vals}
    out: list[str] = []
    section = None
    replaced_indent: int | None = None  # a replaced key whose old value was a block list ("- a" lines below it)
    for line in text.splitlines():
        if replaced_indent is not None:
            stripped = line.lstrip(" ")
            if stripped.startswith("- ") and len(line) - len(stripped) >= replaced_indent:
                continue  # the old block-style list items: the new value was written in flow style on the key line
            replaced_indent = None
        m = _SECTION_LINE.match(line)
        if m:
            section = m.group("key")
            out.append(line)
            continue
        m = _KEY_LINE.match(line)
        if m and section in data and m.group("key") in data[section]:
            key = m.group("key")
            remaining.discard((section, key))
            new_val = _yaml_scalar(data[section][key])
            out.append(f"{m.group('indent')}{key}:{m.group('sp') or ' '}{new_val}{m.group('comment') or ''}")
            replaced_indent = len(m.group("indent"))
            continue
        out.append(line)
    if remaining:
        out = _insert_missing(out, data, remaining)
    return "\n".join(out) + "\n", True


def _insert_missing(lines: list[str], data: dict[str, dict[str, Any]], remaining: set) -> list[str]:
    """Keys an older config.yaml doesn't have yet (new features): add them at the end of their section — or add the
    whole section at the end of the file — instead of rewriting the file and losing its comments."""
    by_section: dict[str, list[str]] = {}
    for s, k in sorted(remaining, key=lambda x: list(data[x[0]]).index(x[1])):
        by_section.setdefault(s, []).append(k)
    headers = {m.group("key"): i for i, ln in enumerate(lines) if (m := _SECTION_LINE.match(ln))}
    inserts: list[tuple[int, list[str]]] = []
    tail: list[str] = []
    for s in sorted(by_section, key=lambda x: list(data).index(x)):
        new = [f"  {k}: {_yaml_scalar(data[s][k])}" for k in by_section[s]]
        if s in headers:
            start = headers[s]
            later = [i for i in headers.values() if i > start]
            end = min(later) if later else len(lines)
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1  # before the blank line(s) that separate sections
            inserts.append((end, new))
        else:
            tail += ["", f"{s}:", *new]
    for pos, new in sorted(inserts, reverse=True):
        lines[pos:pos] = new
    while lines and not lines[-1].strip():
        lines.pop()
    return lines + tail


def save_config(cfg: Config, path: Path | str | None = None) -> None:
    """Write the config back to YAML (Settings page), keeping the comments in config.yaml."""
    path = Path(path) if path else config_path()
    data = cfg.model_dump(mode="json")
    if path.exists():
        text, complete = update_yaml_text(path.read_text(encoding="utf-8"), data)
        if complete:
            path.write_text(text, encoding="utf-8")
            return
    # flow style for lists of scalars (slots: ['12:30', …]) — the in-place updater above edits one line per key
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None), encoding="utf-8")


def load_secrets() -> Secrets:
    return Secrets()


def save_secret(name: str, value: str) -> None:
    """Upsert KEY=value in .env and the current process environment."""
    name = name.upper()
    if any(ch in value or ch in name for ch in "\r\n"):
        raise ValueError("API keys must be a single line")
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
    if os.name != "nt":
        try:
            os.chmod(ENV_PATH, 0o600)  # secrets: only the ClipForge user may read them (other server users can't)
        except OSError:
            pass


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[:4]}…{value[-2:]}" if len(value) > 8 else "••••"

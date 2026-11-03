"""`clipforge doctor` — check every external dependency and say exactly how to fix what's missing."""

from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

from clipforge.config import FRONTEND_DIST, load_config, load_secrets


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _os() -> str:
    s = platform.system()
    return {"Darwin": "macos", "Windows": "windows"}.get(s, "linux")


FIXES = {
    "ffmpeg": {
        "windows": "winget install --id Gyan.FFmpeg -e   (then open a NEW terminal)",
        "macos": "brew install ffmpeg",
        "linux": "sudo apt install ffmpeg   (or your distro's package manager)",
    },
    "node": {
        "windows": "winget install OpenJS.NodeJS.LTS",
        "macos": "brew install node",
        "linux": "sudo apt install nodejs npm   (or use nvm)",
    },
    "ollama": {
        "windows": "Download from https://ollama.com/download  then: ollama pull qwen2.5:7b",
        "macos": "brew install ollama && ollama serve & ollama pull qwen2.5:7b",
        "linux": "curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen2.5:7b",
    },
}


def run_checks(fix_assets: bool = False) -> list[Check]:
    """Run all checks. With `fix_assets`, missing fonts / face models are downloaded."""
    osname = _os()
    checks: list[Check] = []
    cfg = load_config()

    # Python
    ok = sys.version_info >= (3, 11)
    checks.append(Check("Python ≥ 3.11", ok, platform.python_version(),
                        "Install Python 3.11 or 3.12 from python.org and recreate the venv"))

    # Python packages
    missing = []
    for mod in ["faster_whisper", "yt_dlp", "scenedetect", "cv2", "mediapipe", "fastapi", "sqlmodel", "typer", "httpx"]:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    checks.append(Check("Python packages", not missing, "all installed" if not missing else f"missing: {', '.join(missing)}",
                        "pip install -r requirements.txt"))

    # ffmpeg
    from clipforge.utils import ffmpeg as ff

    try:
        ver = ff.version()
        ff.ffprobe_bin()
        libass = ff.has_filter("ass")
        short = (ver or "").replace("ffmpeg version ", "").split(" Copyright")[0]
        checks.append(Check("ffmpeg + ffprobe", bool(ver), short[:40], FIXES["ffmpeg"][osname]))
        checks.append(Check("ffmpeg libass (captions)", libass, "ass filter available" if libass else "ffmpeg built without libass",
                            "Install a 'full' ffmpeg build: " + FIXES["ffmpeg"][osname]))
    except Exception as exc:
        checks.append(Check("ffmpeg + ffprobe", False, str(exc).splitlines()[0], FIXES["ffmpeg"][osname]))

    # yt-dlp
    try:
        import yt_dlp

        checks.append(Check("yt-dlp", True, yt_dlp.version.__version__, "pip install -U yt-dlp"))
    except Exception:
        checks.append(Check("yt-dlp", False, "not installed", "pip install -U yt-dlp"))

    # GPU
    from clipforge.utils.gpu import cuda_available, nvenc_available

    cuda = cuda_available()
    checks.append(Check("CUDA GPU (Whisper)", cuda, "CUDA available" if cuda else "CPU only — will use small/int8 (slower)",
                        "Optional: NVIDIA GPU + CUDA 12 + cuDNN 9, or use the Colab notebook", required=False))
    nv = nvenc_available()
    checks.append(Check("NVENC (fast encode)", nv, "h264_nvenc works" if nv else "using libx264 (CPU)",
                        "Optional: NVIDIA GPU with recent driver", required=False))

    # Ollama
    from clipforge.llm.ollama_provider import list_ollama_models

    try:
        models = list_ollama_models(cfg.llm.ollama_url)
        checks.append(Check("Ollama (local LLM)", bool(models),
                            f"models: {', '.join(models)}" if models else "running but no models pulled",
                            "ollama pull qwen2.5:7b", required=cfg.llm.provider == "ollama"))
    except Exception:
        checks.append(Check("Ollama (local LLM)", False, f"not reachable at {cfg.llm.ollama_url}", FIXES["ollama"][osname],
                            required=cfg.llm.provider == "ollama"))

    secrets = load_secrets()
    checks.append(Check("Gemini API key (optional)", bool(secrets.gemini_api_key),
                        "set" if secrets.gemini_api_key else "not set",
                        "Free key: https://aistudio.google.com/apikey  → add GEMINI_API_KEY=... to .env", required=False))
    checks.append(Check("Groq API key (optional)", bool(secrets.groq_api_key),
                        "set" if secrets.groq_api_key else "not set",
                        "Free key: https://console.groq.com/keys  → add GROQ_API_KEY=... to .env", required=False))

    # Fonts / models
    from clipforge.utils import assets

    if fix_assets:
        try:
            assets.ensure_fonts()
        except Exception as exc:  # report, don't crash doctor
            checks.append(Check("Font download", False, str(exc), "Check your internet connection"))
    miss = assets.missing_fonts()
    checks.append(Check("Caption fonts", not miss, "all present" if not miss else f"missing: {', '.join(miss)}",
                        "clipforge doctor --fix"))
    if fix_assets:
        for name in assets.MODELS:
            try:
                assets.ensure_model(name)
            except Exception as exc:
                checks.append(Check(f"Model {name}", False, str(exc), "Check your internet connection"))
    from clipforge.config import MODELS_DIR

    have = [n for n in assets.MODELS if (MODELS_DIR / n).exists()]
    checks.append(Check("Face detection models", len(have) == len(assets.MODELS),
                        f"{len(have)}/{len(assets.MODELS)} present", "clipforge doctor --fix"))

    # Node / frontend
    node = shutil.which("node")
    node_ver = ""
    if node:
        try:
            node_ver = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
    checks.append(Check("Node.js (only to build the UI)", bool(node), node_ver or "not found", FIXES["node"][osname],
                        required=False))
    built = (FRONTEND_DIST / "index.html").exists()
    checks.append(Check("Web UI built", built, "frontend/dist present" if built else "not built",
                        "cd frontend && npm install && npm run build", required=False))
    return checks

"""Settings, resolved once from the environment (and .env if present)."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .util import ROOT, CfError

PROMPTS_DIR = ROOT / "prompts"


def _load_dotenv() -> None:
    """Real env vars win; .env only fills gaps. 12 lines beats a dependency."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _num(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise CfError(f"{key} must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class Config:
    # llm
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_temperature: float
    openai_timeout_s: float
    llm_max_retries: int
    target_duration_s: int
    # voice
    piper_voice: str
    piper_length_scale: float
    piper_dir: Path
    # images
    comfy_base_url: str
    comfy_workflow: Path
    comfy_checkpoint: str
    comfy_width: int
    comfy_height: int
    comfy_steps: int
    comfy_cfg: float
    comfy_base_seed: int
    comfy_timeout_s: float
    # assembly
    segment_padding_s: float
    fps: int
    crf: int
    fade_s: float
    caption_mode: str
    # retries
    media_max_retries: int

    @property
    def comfy_url(self) -> str:
        return self.comfy_base_url.rstrip("/")


def load_config() -> Config:
    _load_dotenv()
    caption_mode = _env("CAPTION_MODE", "auto") or "auto"
    if caption_mode not in ("auto", "on_screen_text", "off"):
        raise CfError(f"CAPTION_MODE must be auto|on_screen_text|off, got {caption_mode!r}")
    return Config(
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        openai_temperature=_num("OPENAI_TEMPERATURE", 0.7),
        openai_timeout_s=_num("OPENAI_TIMEOUT_S", 180),
        llm_max_retries=int(_num("LLM_MAX_RETRIES", 3)),
        target_duration_s=int(_num("TARGET_DURATION_S", 90)),
        piper_voice=_env("PIPER_VOICE", "en_US-ryan-medium"),
        piper_length_scale=_num("PIPER_LENGTH_SCALE", 1.0),
        piper_dir=ROOT / _env("PIPER_DIR", "models/piper"),
        comfy_base_url=_env("COMFY_BASE_URL", "auto"),
        comfy_workflow=ROOT / _env("COMFY_WORKFLOW", "workflows/stickfigure.json"),
        comfy_checkpoint=_env("COMFY_CHECKPOINT"),
        comfy_width=int(_num("COMFY_WIDTH", 768)),
        comfy_height=int(_num("COMFY_HEIGHT", 432)),
        comfy_steps=int(_num("COMFY_STEPS", 22)),
        comfy_cfg=_num("COMFY_CFG", 6.0),
        comfy_base_seed=int(_num("COMFY_BASE_SEED", 1234)),
        comfy_timeout_s=_num("COMFY_TIMEOUT_S", 180),
        segment_padding_s=_num("SEGMENT_PADDING_S", 0.25),
        fps=int(_num("FPS", 30)),
        crf=int(_num("CRF", 20)),
        fade_s=_num("FADE_S", 0.3),
        caption_mode=caption_mode,
        media_max_retries=int(_num("MEDIA_MAX_RETRIES", 3)),
    )


def require_llm(cfg: Config) -> None:
    if not cfg.openai_api_key:
        raise CfError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in "
            "(or export it) before running the agent stages."
        )


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise CfError(f"required binary not on PATH: {name}")
    return path


def venv_python() -> Path:
    """The interpreter running us - used to invoke piper's CLI in the same env."""
    import sys

    return Path(sys.executable).parent / "piper"
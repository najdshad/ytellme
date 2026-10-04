"""Base script: validation, and the deterministic translation into media scripts.

Nothing here calls a model. The scriptwriter's contract stays simple because the
prompt-consistency rules are enforced in this one place instead of being trusted
to the agent.
"""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from .config import ROOT, Config
from .util import CfError, read_json

SCHEMA_PATH = ROOT / "schema" / "base_script.schema.json"
SUPPORTED_SCHEMA_VERSIONS = {"1.0"}

# Appended to every scene intent. The spec calls for "no text in image"; saying it
# on every prompt rather than only in the negative prompt is the difference
# between usually and reliably.
NO_TEXT_CLAUSE = "no text, no letters, no words, no captions, no watermark, no signature"


class ScriptError(CfError):
    pass


def validate(base: dict, source: str = "script") -> dict:
    """Validate against the schema. All errors at once, none of them a stack trace."""
    version = base.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ScriptError(
            f"{source}: schema_version {version!r} is not supported "
            f"(this build understands {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(base), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"{source}: schema validation failed with {len(errors)} error(s)"]
        for err in errors[:20]:
            where = "/".join(str(p) for p in err.absolute_path) or "<root>"
            lines.append(f"  at {where}: {err.message}")
        if len(errors) > 20:
            lines.append(f"  ... and {len(errors) - 20} more")
        raise ScriptError("\n".join(lines))

    seen: set[str] = set()
    for seg in base["segments"]:
        if seg["segment_id"] in seen:
            raise ScriptError(f"{source}: duplicate segment_id {seg['segment_id']!r}")
        seen.add(seg["segment_id"])
    return base


def load(path: Path) -> dict:
    return validate(read_json(path), source=str(path))


def normalize_scene_intent(scene_intent: str) -> str:
    """scene_intent is written for a human; the image model wants a tighter line.

    ponytail: whitespace + no-text clause only. If prompts need real semantic
    rewriting, add an LLM normaliser here - but keep the style block prepended
    verbatim by this module so consistency can never be delegated away.
    """
    text = re.sub(r"\s+", " ", scene_intent).strip().rstrip(".")
    return f"{text}. {NO_TEXT_CLAUSE}"


def derive_frame_script(base: dict, cfg: Config) -> dict:
    """base script -> the exact image-generation script, one entry per segment."""
    style = base["style_prompt"]
    frames = []
    for seg in base["segments"]:
        frames.append(
            {
                "segment_id": seg["segment_id"],
                "prompt": f"{style}\n\n{normalize_scene_intent(seg['scene_intent'])}",
                "seed": _seed(cfg.comfy_base_seed, seg["segment_id"]),
            }
        )
    prompts = [f["prompt"] for f in frames]
    if len({p.split("\n\n", 1)[0] for p in prompts}) != 1:
        raise ScriptError("style_prompt is not identical across frames - this is a bug in derive")

    script = {
        "schema_version": base["schema_version"],
        "style_prompt": style,
        "negative_prompt": base.get("negative_prompt", ""),
        "checkpoint": cfg.comfy_checkpoint,
        "width": cfg.comfy_width,
        "height": cfg.comfy_height,
        "steps": cfg.comfy_steps,
        "cfg": cfg.comfy_cfg,
        "frames": frames,
    }
    validate_frame_script(script)
    return script


def _seed(base_seed: int, segment_id: str) -> int:
    """Stable per-segment seed: a retry re-rolls the same frame, not a new one."""
    import hashlib

    digest = hashlib.sha1(segment_id.encode("utf-8")).hexdigest()
    return (base_seed + int(digest[:8], 16)) % 2_147_483_647


def validate_frame_script(script: dict) -> dict:
    if not script["frames"]:
        raise ScriptError("frame script has no frames")
    if script["width"] % 8 or script["height"] % 8:
        raise ScriptError(
            f"frame size {script['width']}x{script['height']} must be a multiple of 8 for latent-space models"
        )
    return script


def derive_narration_script(base: dict, cfg: Config) -> dict:
    """base script -> one TTS call per segment. Nothing else crosses the wire."""
    return {
        "schema_version": base["schema_version"],
        "voice": cfg.piper_voice,
        "length_scale": cfg.piper_length_scale,
        "format": "wav",
        "segments": [{"segment_id": s["segment_id"], "text": s["narration"]} for s in base["segments"]],
    }
"""Media stages: local piper narration, ComfyUI frames, and the timing reconciliation.

The two synthesis stages are the same shape - call a service, write a file, prove
the file is real - so they share one retry policy: retry this many times, then
kill the run. A failed segment never becomes a placeholder.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config, require_binary, venv_python
from .util import CfError, ffprobe_duration, log, retry, run

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- voice


def ensure_voice(cfg: Config) -> Path:
    """piper's own downloader, so voice files and their config never drift apart."""
    model = cfg.piper_dir / f"{cfg.piper_voice}.onnx"
    if model.exists() and model.with_suffix(".onnx.json").exists():
        return model
    cfg.piper_dir.mkdir(parents=True, exist_ok=True)
    log(f"downloading piper voice {cfg.piper_voice} from HuggingFace")
    run(
        [venv_python(), "-m", "piper.download_voices", "--download-dir", cfg.piper_dir, cfg.piper_voice],
        timeout=600,
    )
    if not model.exists():
        raise CfError(f"piper voice {cfg.piper_voice} not found after download (is the name valid?)")
    return model


def synthesize_audio(cfg: Config, narration_script: dict, audio_dir: Path) -> list[dict]:
    """One synthesis call per segment. No whole-narration synthesis, ever."""
    require_binary("ffprobe")
    model = ensure_voice(cfg)
    audio_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for seg in narration_script["segments"]:
        sid = seg["segment_id"]
        text = seg["text"].strip()
        out = audio_dir / f"{sid}.wav"

        def once() -> Path:
            _piper(model, out, text, narration_script["length_scale"])
            ffprobe_duration(out)  # raises on empty or corrupt audio
            return out

        retry(once, cfg.media_max_retries, f"tts {sid}")
        entries.append({"segment_id": sid, "audio": out, "duration_s": ffprobe_duration(out)})
        log(f"  {sid}: {entries[-1]['duration_s']:.2f}s  {text[:48]!r}")
    return entries


def _piper(model: Path, out: Path, text: str, length_scale: float) -> None:
    """piper writes to stdout-ish file handles; feed it stdin, not a shell string."""
    import subprocess

    proc = subprocess.run(
        [str(venv_python()), "-m", "piper", "-m", str(model), "-f", str(out), "--length-scale", str(length_scale)],
        input=text + "\n",
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise CfError(f"piper failed ({proc.returncode}): " + " | ".join(tail))


# --------------------------------------------------------------------------- frames


def _candidates(cfg: Config) -> list[str]:
    if cfg.comfy_base_url != "auto":
        return [cfg.comfy_base_url]
    hosts = ["127.0.0.1", "localhost"]
    gateway = _default_gateway()
    if gateway:
        hosts.append(gateway)
    nameserver = _first_nameserver()
    if nameserver and nameserver not in hosts:
        hosts.append(nameserver)
    return [f"http://{host}:8188" for host in hosts]


def _default_gateway() -> str | None:
    proc = run(["ip", "route", "show", "default"], check=False)
    parts = proc.stdout.split()
    return parts[parts.index("via") + 1] if "via" in parts else None


def _first_nameserver() -> str | None:
    resolv = Path("/etc/resolv.conf")
    if not resolv.exists():
        return None
    for line in resolv.read_text(encoding="utf-8").splitlines():
        if line.startswith("nameserver"):
            return line.split()[1]
    return None


def comfy_base(cfg: Config) -> str:
    """Probe candidate hosts. On WSL2 that is localhost when mirrored networking is
    on, otherwise the Windows default gateway."""
    tried = []
    for base in _candidates(cfg):
        try:
            with urllib.request.urlopen(f"{base}/system_stats", timeout=3) as response:
                if response.status == 200:
                    if tried:
                        log(f"comfyui reachable at {base}")
                    return base
        except Exception as exc:  # noqa: BLE001 - probing several hosts by design
            tried.append(f"{base} ({type(exc).__name__})")
    raise CfError(
        "cannot reach ComfyUI. Tried: " + ", ".join(tried) + "\n"
        "  If ComfyUI runs on the Windows host from WSL2 it must listen on all interfaces:\n"
        "    python main.py --listen 0.0.0.0 --port 8188\n"
        "  and Windows Firewall must allow inbound 8188 (or run WSL2 with mirrored\n"
        "  networking, which makes 127.0.0.1 work directly).\n"
        "  Then set COMFY_BASE_URL to the working URL."
    )


def _get_json(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CfError(f"HTTP {exc.code} from {url}: {exc.read().decode('utf-8', 'replace')[:400]}") from None
    except urllib.error.URLError as exc:
        raise CfError(f"cannot reach {url}: {exc.reason}") from None


def load_workflow(path: Path, cfg: Config) -> dict:
    """An "Export (API)" workflow with {{TOKEN}} placeholders.

    Node ids are data, not code: the user exports the workflow once and this
    module substitutes values, so a ComfyUI graph change needs no Python edit.
    """
    if not path.exists():
        raise CfError(
            f"no ComfyUI workflow at {path}\n"
            "  In ComfyUI: Workflow menu -> Export (API) -> save as workflows/stickfigure.json\n"
            "  Tokens it may use: {{POSITIVE}} {{NEGATIVE}} {{SEED}} {{WIDTH}} {{HEIGHT}} {{CHECKPOINT}}"
        )
    template = json.loads(path.read_text(encoding="utf-8"))
    graph = {k: v for k, v in template.items() if not k.startswith("_cf_")}
    blob = json.dumps(graph)
    if "{{POSITIVE}}" not in blob:
        raise CfError(f"{path} has no {{{{POSITIVE}}}} token - export a workflow whose CLIP text node takes the prompt")
    if "{{CHECKPOINT}}" in blob and not cfg.comfy_checkpoint:
        raise CfError(
            f"{path} expects a {{{{CHECKPOINT}}}} token but COMFY_CHECKPOINT is empty. "
            "Either set COMFY_CHECKPOINT in .env, or bake the model name into the workflow."
        )
    return template


def _render(template, mapping: dict):
    """Recursively replace {{TOKEN}} in every string of the workflow graph."""
    if isinstance(template, str):
        for key, value in mapping.items():
            template = template.replace("{{" + key + "}}", str(value))
        return template
    if isinstance(template, list):
        return [_render(item, mapping) for item in template]
    if isinstance(template, dict):
        return {key: _render(value, mapping) for key, value in template.items()}
    return template


def _pick_output_image(history_entry: dict, preferred_node: str | None) -> dict:
    outputs = history_entry.get("outputs") or {}
    if preferred_node and preferred_node in outputs:
        images = outputs[preferred_node].get("images") or []
        if images:
            return images[0]
    for node_output in outputs.values():
        images = node_output.get("images") or []
        if images:
            return images[0]
    raise CfError(f"ComfyUI finished but produced no image (outputs: {list(outputs)})")


def generate_frames(cfg: Config, frame_script: dict, frames_dir: Path) -> list[dict]:
    base = comfy_base(cfg)
    template = load_workflow(cfg.comfy_workflow, cfg)
    preferred_node = template.get("_cf_output_node")
    client_id = str(uuid.uuid4())
    frames_dir.mkdir(parents=True, exist_ok=True)

    def render_one(frame: dict) -> Path:
        out = frames_dir / f"{frame['segment_id']}.png"
        graph = _render(
            {k: v for k, v in template.items() if not k.startswith("_cf_")},
            {
                "POSITIVE": frame["prompt"],
                "NEGATIVE": frame_script["negative_prompt"],
                "SEED": frame["seed"],
                "WIDTH": frame_script["width"],
                "HEIGHT": frame_script["height"],
                "CHECKPOINT": frame_script["checkpoint"],
                "STEPS": frame_script["steps"],
                "CFG": frame_script["cfg"],
            },
        )
        payload = json.dumps({"prompt": graph, "client_id": client_id}).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                prompt_id = json.loads(response.read().decode("utf-8"))["prompt_id"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # ComfyUI answers 400 with per-node validation errors - print the useful part.
            raise CfError(f"ComfyUI rejected the workflow: {detail[:800]}") from None

        deadline = time.monotonic() + cfg.comfy_timeout_s
        entry = None
        while time.monotonic() < deadline:
            time.sleep(1.0)
            history = _get_json(f"{base}/history/{prompt_id}", timeout=30)
            if prompt_id in history:
                entry = history[prompt_id]
                break
        if entry is None:
            raise CfError(f"ComfyUI did not finish {frame['segment_id']} within {cfg.comfy_timeout_s:.0f}s")
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            messages = [m for m in (status.get("messages") or []) if m[0] == "execution_error"]
            detail = messages[0][1] if messages else status
            raise CfError(f"ComfyUI execution error on {frame['segment_id']}: {str(detail)[:600]}")

        image_info = _pick_output_image(entry, preferred_node)
        query = urllib.parse.urlencode(
            {
                "filename": image_info["filename"],
                "subfolder": image_info.get("subfolder", ""),
                "type": image_info.get("type", "output"),
            }
        )
        with urllib.request.urlopen(f"{base}/view?{query}", timeout=120) as response:
            data = response.read()
        if not data.startswith(PNG_MAGIC):
            raise CfError(f"ComfyUI returned {len(data)} bytes that are not a PNG for {frame['segment_id']}")
        out.write_bytes(data)
        return out

    entries = []
    for frame in frame_script["frames"]:
        path = retry(lambda f=frame: render_one(f), cfg.media_max_retries, f"image {frame['segment_id']}")
        entries.append({"segment_id": frame["segment_id"], "image": path, "seed": frame["seed"]})
        log(f"  {frame['segment_id']}: {path.stat().st_size // 1024}kB seed={frame['seed']}")
    return entries


# --------------------------------------------------------------------------- timing


@dataclass
class Clip:
    """Everything the assembler is allowed to know about one segment."""

    segment_id: str
    image: Path
    audio: Path
    audio_s: float  # measured, drives captions and the pacing report
    duration_s: float  # audio + padding, drives the video frame
    transition: str
    caption: str | None
    start_s: float
    hint_s: float


def build_timeline(
    base: dict, audio_entries: list[dict], frame_entries: list[dict], padding_s: float, caption_mode: str
) -> list[Clip]:
    """Audio is the master clock. The hint is reported, never obeyed."""
    audio = {e["segment_id"]: e for e in audio_entries}
    frames = {e["segment_id"]: e for e in frame_entries}
    clips = []
    cursor = 0.0
    for seg in base["segments"]:
        sid = seg["segment_id"]
        if sid not in audio or sid not in frames:
            missing = sid if sid not in audio else f"frame for {sid}"
            raise CfError(
                f"media manifest is incomplete: missing {missing}. "
                "Re-run the media stage for this run; no placeholder frames are allowed."
            )
        caption = seg.get("on_screen_text")
        if caption_mode == "off":
            caption = None
        elif caption_mode == "auto" and not caption:
            caption = seg["narration"]
        clips.append(
            Clip(
                segment_id=sid,
                image=frames[sid]["image"],
                audio=audio[sid]["audio"],
                audio_s=audio[sid]["duration_s"],
                duration_s=audio[sid]["duration_s"] + padding_s,
                transition=seg["transition"],
                caption=caption,
                start_s=cursor,
                hint_s=seg["duration_hint_s"],
            )
        )
        cursor += audio[sid]["duration_s"] + padding_s
    return clips


def pad_audio(clips: list[Clip], media_dir: Path) -> None:
    """Append silence so each clip's video frame and audio end together.

    Video would otherwise drift by n * padding against the concatenated audio.
    """
    for clip in clips:
        padded = media_dir / "audio" / f"{clip.segment_id}.pad.wav"
        if not padded.exists():
            run(
                [
                    "ffmpeg", "-v", "error", "-y",
                    "-i", clip.audio,
                    "-af", f"apad=pad_dur={clip.duration_s - clip.audio_s:.4f}",
                    "-t", f"{clip.duration_s:.4f}",
                    "-c:a", "pcm_s16le",
                    padded,
                ],
                timeout=120,
            )
        clip.audio = padded


def pacing_report(base: dict, clips: list[Clip], out_path: Path) -> None:
    """hint vs actual. Feedback for the scriptwriter prompt; never fed to the assembler."""
    lines = [
        "# Pacing report",
        "",
        "hint = scriptwriter estimate, actual = measured piper audio. Advisory only.",
        "",
        "| segment | hint | actual | delta | words |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    total_hint = total_actual = 0.0
    for seg, clip in zip(base["segments"], clips):
        words = len(seg["narration"].split())
        total_hint += clip.hint_s
        total_actual += clip.audio_s
        lines.append(
            f"| {clip.segment_id} | {clip.hint_s:.1f} | {clip.audio_s:.1f} | "
            f"{clip.audio_s - clip.hint_s:+.1f} | {words} |"
        )
    drift = total_actual - total_hint
    lines += [
        "",
        f"total: hint {total_hint:.1f}s, actual {total_actual:.1f}s, drift {drift:+.1f}s "
        f"({drift / total_hint * 100:+.1f}%)",
        "",
        "A consistent one-sided drift means the words-per-second constant in the "
        "scriptwriter prompt is wrong, not that the writer miscounted.",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
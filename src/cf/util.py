"""Shared primitives: logging, retries, subprocess, json, ffprobe."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CfError(Exception):
    """Anything the human is meant to read and act on."""


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def retry(fn, attempts: int, label: str):
    """Call fn(); on failure retry up to `attempts` times with capped backoff.

    Per-segment media failures retry in isolation; a run that exhausts them dies
    loudly rather than shipping a placeholder frame.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, we re-raise below
            last = exc
            detail = str(exc).strip().splitlines()
            detail = detail[0] if detail else exc.__class__.__name__
            if attempt == attempts:
                break
            wait = min(2 ** attempt, 15)
            log(f"{label}: attempt {attempt}/{attempts} failed ({detail}) - retrying in {wait}s")
            time.sleep(wait)
    raise CfError(f"{label}: failed after {attempts} attempts - {last}") from last


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "untitled"


def run(cmd, cwd: Path | None = None, check: bool = True, timeout: float | None = None):
    """subprocess.run with output captured, so failures carry ffmpeg's own words."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise CfError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd[:4])}...\n  " + "\n  ".join(tail))
    return proc


def ffprobe_duration(path: Path) -> float:
    """Measured duration in seconds. The master clock for the whole assembly."""
    proc = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=False,
    )
    try:
        seconds = float(proc.stdout.strip())
    except ValueError:
        raise CfError(f"unreadable audio, ffprobe found no duration: {path}") from None
    if seconds <= 0.05:
        raise CfError(f"empty or corrupt audio clip ({seconds:.3f}s): {path}")
    return seconds


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CfError(f"missing artifact: {path}") from None
    except json.JSONDecodeError as exc:
        raise CfError(f"{path} is not valid JSON: {exc}") from None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def rel(path: Path, base: Path) -> str:
    """Path relative to `base`, for ffmpeg inputs run with cwd=base.

    Keeps concat lists and filtergraphs free of absolute paths (and of the
    escaping bugs that come with them).
    """
    return path.resolve().relative_to(base.resolve()).as_posix()


def esc_concat(path: str) -> str:
    """Quote a path for an ffmpeg concat demuxer list."""
    return "'" + path.replace("'", r"'\''") + "'"


def esc_filter_value(value: str) -> str:
    """Escape a value interpolated into an ffmpeg filtergraph argument."""
    return value.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def die(msg: str):
    print(f"\nerror: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)
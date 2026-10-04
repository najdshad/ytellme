"""FFmpeg assembler. No creative logic: it renders the timeline it is handed.

Contract, per the spec: input is the base script's segment list plus per segment
(image, audio, measured duration, transition, on_screen_text). It owns no naming,
no timing decisions and no content.
"""

import json
import math
from pathlib import Path

from .config import Config
from .media import Clip
from .script import validate
from .util import CfError, esc_concat, esc_filter_value, rel, run, write_json

# The paper colour the style block asks for, so upscaling never letterboxes black.
PAPER = "0xF2EFE6"


def assemble(base: dict, clips: list[Clip], cfg: Config, run_dir: Path, out_name: str) -> Path:
    validate(base, source="assemble")
    if not clips:
        raise CfError("nothing to assemble: no clips in the timeline")
    for clip in clips:
        for path, label in ((clip.image, "image"), (clip.audio, "audio")):
            if not path.exists():
                raise CfError(f"missing {label} artifact for {clip.segment_id}: {path}")

    media_dir = run_dir / "media"
    output_dir = run_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    width, height = base["width"], base["height"]

    # Audio: the concat demuxer, no duration directives. Real wavs carry their own
    # lengths, so it concatenates exactly. (A duration directive on a *final*
    # entry is silently inherited from the previous one - measured, not assumed.)
    audio_list = media_dir / "audio.txt"
    audio_list.write_text(_audio_list([rel(c.audio, media_dir) for c in clips]), encoding="utf-8")

    # Video: one looped still per input, trimmed to its measured duration, joined by
    # the concat filter. This is the only construction measured to honour an
    # arbitrary final duration exactly; the concat *demuxer* cannot.
    per_input = ["scale={0}:{1}:force_original_aspect_ratio=decrease".format(width, height),
                 "pad={0}:{1}:(ow-iw)/2:(oh-ih)/2:color={2}".format(width, height, PAPER),
                 "setsar=1", "format=yuv420p", f"fps={cfg.fps}"]
    graph = [f"[{i}:v]{','.join(per_input)}[v{i}]" for i in range(len(clips))]
    graph.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[vcat]")

    tail = list(per_input)
    tail += _fade_filters(clips, cfg.fade_s)

    srt_path = output_dir / "captions.srt"
    if any(clip.caption for clip in clips):
        srt_path.write_text(build_srt(clips), encoding="utf-8")
        # Outline text, not a box: these frames are paper-white, and an opaque
        # box reads as a bug rather than a subtitle.
        style = (
            "FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,MarginV=48"
        )
        tail.append(f"subtitles={esc_filter_value(srt_path.as_posix())}:force_style='{style}'")
    graph.append(f"[vcat]{','.join(tail)}[vout]")

    out_path = output_dir / out_name
    command = ["ffmpeg", "-v", "error", "-y"]
    for clip in clips:
        command += ["-loop", "1", "-t", f"{clip.duration_s:.4f}", "-i", rel(clip.image, run_dir)]
    command += [
        "-f", "concat", "-safe", "0", "-i", rel(audio_list, run_dir),
        "-filter_complex", ";".join(graph),
        "-map", "[vout]", "-map", f"{len(clips)}:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg.crf),
        "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    run(command, cwd=run_dir, timeout=3600)
    write_json(output_dir / "timings.json", _timings(clips))
    return out_path


def contact_sheet(clips: list[Clip], run_dir: Path) -> Path | None:
    """One image of every frame, so the human gate can judge style drift at a glance."""
    if not clips:
        return None
    media_dir = run_dir / "media"
    listing = media_dir / "sheet.txt"
    # Concat entries resolve against the *list file's* directory, so these are
    # relative to media/, not to media/frames/.
    listing.write_text(_audio_list([rel(c.image, media_dir) for c in clips]), encoding="utf-8")
    cols = 4
    rows = max(1, min(math.ceil(len(clips) / cols), 10))
    out = run_dir / "output" / "contact_sheet.png"
    run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", rel(listing, run_dir),
            "-vf", f"scale=320:-1,tile={cols}x{rows}:padding=4:color=white",
            "-frames:v", "1",
            "output/contact_sheet.png",
        ],
        cwd=run_dir,
        timeout=600,
    )
    return out


def _audio_list(paths: list[str]) -> str:
    """Concat demuxer list with no duration directives: one entry per file, once."""
    return "".join(f"file {esc_concat(path)}\n" for path in paths)


def _fade_filters(clips: list[Clip], fade_s: float) -> list[str]:
    """Dip-to-black, stacked on one input via absolute st.

    ponytail: this is not a crossfade - real crossfades need one clip per segment
    and an xfade graph, which is fragile and buys little on still frames. Upgrade
    path: if segments need to blend, split per-segment clips and chain xfade.
    """
    if fade_s <= 0:
        return []
    filters = [f"fade=t=in:st={clip.start_s:.3f}:d={fade_s:.3f}" for clip in clips if clip.transition == "fade"]
    total = clips[-1].start_s + clips[-1].duration_s
    if total > fade_s:
        filters.append(f"fade=t=out:st={total - fade_s:.3f}:d={fade_s:.3f}")
    return filters


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def build_srt(clips: list[Clip]) -> str:
    """Captions cover the narration, not the trailing padding silence."""
    cues = []
    for clip in clips:
        if not clip.caption:
            continue
        text = " ".join(clip.caption.split())
        cues.append(
            f"{len(cues) + 1}\n{_srt_time(clip.start_s)} --> {_srt_time(clip.start_s + clip.audio_s)}\n{text}\n"
        )
    return "\n".join(cues)


def _timings(clips: list[Clip]) -> dict:
    total = clips[-1].start_s + clips[-1].duration_s
    return {
        "total_s": round(total, 3),
        "segments": [
            {
                "segment_id": c.segment_id,
                "start_s": round(c.start_s, 3),
                "audio_s": round(c.audio_s, 3),
                "padded_s": round(c.duration_s, 3),
                "hint_s": c.hint_s,
                "transition": c.transition,
                "caption": c.caption,
            }
            for c in clips
        ],
    }


def probe_output(path: Path) -> dict:
    """Sanity-check the rendered file: it must exist, be non-trivial, and be playable."""
    if not path.exists() or path.stat().st_size < 10_000:
        raise CfError(f"assembler produced no usable video: {path}")
    proc = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
            "-of", "json", path,
        ],
        check=False,
    )
    return json.loads(proc.stdout or "{}")
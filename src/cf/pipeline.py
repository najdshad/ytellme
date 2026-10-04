"""CLI: cf run / cf doctor.

Each stage writes one artifact to the run directory, so the pipeline resumes from
any stage. An image failure never re-runs research; a caption fix never re-rolls
a frame.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import agents, assemble as asm, media
from .config import Config, ROOT, load_config, require_binary, require_llm
from .script import derive_frame_script, derive_narration_script, load
from .util import CfError, die, log, read_json, slugify, step, write_json, write_text

STAGES = ("research", "story", "script", "derive", "audio", "frames", "assemble")
NOTES_TEMPLATE = """# Test run notes - {slug}

Prompts: {prompts}. Review the draft cut and the contact sheet, then fill this in.

- [ ] narration quality (natural, no filler, right pace)
- [ ] frame consistency (same characters, same drawing style across all frames)
- [ ] pacing (nothing drags, nothing is rushed)
- [ ] transitions (cut where it should cut, fade only where it earns it)
- [ ] captions (readable, correct, not fighting the visual)

## What dragged

## What drifted

## What felt flat

## Changes to make in the prompt layer

VERDICT: (approve | revise)
"""


def run_dir_for(slug: str) -> Path:
    return ROOT / "runs" / slug


def log_line(run_dir: Path, message: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with (run_dir / "run.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {message}\n")


def do_run(cfg: Config, args) -> int:
    slug = args.slug or slugify(args.topic)
    run_dir = run_dir_for(slug)
    for sub in ("research", "story", "script", "media/audio", "media/frames", "output"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    report = run_dir / "research" / "report.md"
    story = run_dir / "story" / "story.md"
    base_path = run_dir / "script" / "base_script.json"
    frame_path = run_dir / "script" / "frame_script.json"
    narration_path = run_dir / "script" / "narration_script.json"
    audio_manifest = run_dir / "media" / "audio.json"
    frames_manifest = run_dir / "media" / "frames.json"
    notes = run_dir / "NOTES.md"

    if args.mode == "full" and not _approved(notes):
        die(
            f"full generation is gated on a human review of the test run.\n"
            f"  {notes} has no 'VERDICT: approve'.\n"
            f"  Run: cf run {args.topic!r} --mode test   then watch output/draft_cut.mp4"
        )

    start = STAGES.index(args.from_stage)
    log_line(run_dir, f"run start topic={args.topic!r} mode={args.mode} prompts={agents.PROMPT_VERSION}")

    if "research" in STAGES[start:] and not (report.exists() and not args.force):
        step("research")
        require_llm(cfg)
        write_text(report, agents.research(cfg, args.topic))
        log_line(run_dir, "research ok (researcher prompt)")
    elif report.exists():
        log("skip research (artifact exists)")

    if "story" in STAGES[start:] and not (story.exists() and not args.force):
        step("story")
        write_text(story, agents.edit(cfg, args.topic, report.read_text(encoding="utf-8")))
        log_line(run_dir, "story ok (editor prompt)")
    elif story.exists():
        log("skip story (artifact exists)")

    if "script" in STAGES[start:] and not (base_path.exists() and not args.force):
        step("script")
        write_json(base_path, agents.script(cfg, story.read_text(encoding="utf-8")))
        log_line(run_dir, "script ok (scriptwriter prompt)")
    elif base_path.exists():
        log("skip script (artifact exists)")

    base = load(base_path)

    if "derive" in STAGES[start:] and not (frame_path.exists() and not args.force):
        step("derive")
        write_json(frame_path, derive_frame_script(base, cfg))
        write_json(narration_path, derive_narration_script(base, cfg))
        log(f"{len(base['segments'])} segments derived")
    elif frame_path.exists():
        log("skip derive (artifact exists)")

    if "audio" in STAGES[start:] and not (audio_manifest.exists() and not args.force):
        step("audio")
        entries = media.synthesize_audio(cfg, read_json(narration_path), run_dir / "media" / "audio")
        write_json(audio_manifest, _portable(entries, run_dir, "audio"))
        log_line(run_dir, "audio ok")
    elif audio_manifest.exists():
        log("skip audio (artifact exists)")

    if "frames" in STAGES[start:] and not (frames_manifest.exists() and not args.force):
        step("frames")
        entries = media.generate_frames(cfg, read_json(frame_path), run_dir / "media" / "frames")
        write_json(frames_manifest, _portable(entries, run_dir, "image"))
        log_line(run_dir, "frames ok")
    elif frames_manifest.exists():
        log("skip frames (artifact exists)")

    sheet = None
    if "assemble" in STAGES[start:]:
        step("assemble")
        clips = media.build_timeline(
            base,
            _resolve(read_json(audio_manifest), run_dir, "audio"),
            _resolve(read_json(frames_manifest), run_dir, "image"),
            cfg.segment_padding_s,
            cfg.caption_mode,
        )
        media.pad_audio(clips, run_dir / "media")
        media.pacing_report(base, clips, run_dir / "output" / "pacing_report.md")
        out_name = "draft_cut.mp4" if args.mode == "test" else "final.mp4"
        video = asm.assemble(base, clips, cfg, run_dir, out_name)
        sheet = asm.contact_sheet(clips, run_dir)
        probed = asm.probe_output(video)
        log_line(run_dir, f"assemble ok -> {out_name}")
        print(f"\nvideo:      {video}")
        print(f"timings:    {run_dir / 'output' / 'timings.json'}")
        print(f"pacing:     {run_dir / 'output' / 'pacing_report.md'}")
        if sheet:
            print(f"frames:     {sheet}")
        print(f"duration:   {float(probed.get('format', {}).get('duration', 0)):.1f}s")

    if args.mode == "test" and not notes.exists():
        write_text(notes, NOTES_TEMPLATE.format(slug=slug, prompts=agents.PROMPT_VERSION))
        print(f"\nHuman gate: watch {run_dir / 'output' / 'draft_cut.mp4'} and {sheet}")
        print(f"Then edit {notes} and set VERDICT: approve to allow --mode full.")
    return 0


def _portable(entries: list[dict], run_dir: Path, key: str) -> list[dict]:
    """Manifests store run-relative paths, so a run directory can be moved."""
    return [{**entry, key: Path(entry[key]).relative_to(run_dir).as_posix()} for entry in entries]


def _resolve(entries: list[dict], run_dir: Path, key: str) -> list[dict]:
    return [{**entry, key: run_dir / entry[key]} for entry in entries]


def _approved(notes: Path) -> bool:
    return notes.exists() and "VERDICT: approve" in notes.read_text(encoding="utf-8")


def doctor(cfg: Config) -> int:
    step("doctor")
    ok = True

    for binary in ("ffmpeg", "ffprobe"):
        try:
            require_binary(binary)
            log(f"ok    {binary}")
        except CfError as exc:
            ok = False
            log(f"FAIL  {exc}")

    if cfg.openai_api_key:
        log(f"ok    OPENAI_API_KEY set, model {cfg.openai_model}")
    else:
        ok = False
        log("FAIL  OPENAI_API_KEY not set - copy .env.example to .env and fill it in")

    for agent in agents.AGENTS:
        path = agents.prompt_path(agent)
        log(f"ok    prompt {path.relative_to(ROOT)}")

    try:
        model = media.ensure_voice(cfg)
        log(f"ok    piper voice {model.name} ({model.stat().st_size // (1024 * 1024)}MB)")
    except CfError as exc:
        ok = False
        log(f"FAIL  {exc}")

    try:
        media.load_workflow(cfg.comfy_workflow, cfg)
        log(f"ok    comfyui workflow {cfg.comfy_workflow.relative_to(ROOT)}")
    except CfError as exc:
        ok = False
        log(f"FAIL  {exc}")

    try:
        base = media.comfy_base(cfg)
        log(f"ok    comfyui at {base}")
    except CfError as exc:
        ok = False
        log(f"FAIL  {str(exc).splitlines()[0]}")

    print("\nall good" if ok else "\nsome checks failed (see above)")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cf", description="Topic -> narrated stick-figure explainer video")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_cmd = sub.add_parser("run", help="run the pipeline for one topic")
    run_cmd.add_argument("topic")
    run_cmd.add_argument("--mode", choices=("test", "full"), default="test")
    run_cmd.add_argument("--slug", help="run directory name (default: slugified topic)")
    run_cmd.add_argument("--from", dest="from_stage", choices=STAGES, default=STAGES[0])
    run_cmd.add_argument("--force", action="store_true", help="re-run every stage in range")

    sub.add_parser("doctor", help="check ffmpeg, keys, prompts, piper voice and ComfyUI")

    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        return doctor(cfg) if args.cmd == "doctor" else do_run(cfg, args)
    except CfError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        die("interrupted")


if __name__ == "__main__":
    sys.exit(main())
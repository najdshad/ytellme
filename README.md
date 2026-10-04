# content-factory

Turn a topic into a finished narrated explainer video.

Give it `"why electricity isn't a fuel"` and it produces an MP4: a narrator
speaking over simple stick-figure illustrations, with burned-in captions. The
writing, the story structure, the visuals, the voice and the edit are all
generated; you supply the topic and a human review at one gate.

```
topic ─▶ research ─▶ story ─▶ script ─▶ derive ─┬─▶ audio ─┐
                                                 └─▶ frames ─┴─▶ assemble ─▶ video
```

---

## What the pieces are

| Stage | What it does | Output |
| --- | --- | --- |
| **Researcher** | Explores the topic broadly: mechanisms, numbers, misconceptions, counterarguments, candidate angles. Deliberately exhaustive — it does *not* pre-digest for a story. | `research/report.md` |
| **Editor / Storywriter** | Mines the report for one angle and a hook → build → payoff arc. Also decides what to cut, explicitly. Adds no new claims. | `story/story.md` |
| **Scriptwriter** | Turns the story into the base script: narration lines, scene intents, transitions, pacing hints. | `script/base_script.json` |
| **Derive** *(code, no model)* | Translates the base script into two specialised scripts and enforces the style rules mechanically. | `script/frame_script.json`, `script/narration_script.json` |
| **Voice** | One local piper synthesis call per segment, plus the measured duration of each clip. | `media/audio/*.wav` |
| **Frames** | One ComfyUI generation per segment, styled by the script's style block. | `media/frames/*.png` |
| **Assemble** | ffmpeg: hold each frame for its audio, join, fade, burn captions. | `output/draft_cut.mp4` |

## The four ideas worth knowing

**1. The base script is the contract.** `script/base_script.json` is the only
artifact downstream stages read, and it is strict schema-validated JSON — not
prose. Everything the editor decided (narration, what the frame shows, which
transition, optional on-screen text) lives there, so the assembler has no
creative logic at all. Unknown schema versions are rejected.

**2. Audio is the master clock.** The script carries intent, never timing. Each
frame is held for the *measured* duration of its narration, so video cannot drift
from voice no matter how wrong the writer's pacing guess was. The scriptwriter's
`duration_hint_s` is advisory: it is compared against reality in
`output/pacing_report.md` and fed back into the prompt, never into the assembler.

**3. Consistency is enforced, not requested.** The header's `style_prompt` block
is prepended byte-identically to every image prompt by the derive step, and a test
asserts it never varies. The scriptwriter writes `scene_intent` for a human; code
normalises it into the image model's register, so the scriptwriter's job stays
simple and the style rules can't be quietly dropped by a model having a bad day.

**4. The prompt layer is a first-class component.** Each agent's system prompt is
a versioned file in `prompts/`, with few-shot examples and a changelog. The test
run is the experiment, the review notes are the result, and prompt edits are
diffable and attributable.

## Design decisions locked in

- **Per-segment synthesis.** Narration is split and synthesised one segment at a
  time — no forced alignment, no whole-voice-over-then-slice step.
- **A human gate.** `--mode test` stops at a draft cut. `--mode full` refuses to
  run until the review notes say `VERDICT: approve`.
- **No silent fallbacks.** A failed frame or clip retries a few times, then kills
  the run with a readable report. A placeholder never reaches the final video.
- **Every stage is resumable.** Each writes one artifact and skips itself if that
  artifact exists, so re-rendering frames never re-runs research.

---

## Setup

Requires Python 3.12+, `ffmpeg`/`ffprobe` on PATH, and a running ComfyUI.

```bash
cp .env.example .env      # then set OPENAI_API_KEY and COMFY_CHECKPOINT
```

The Python environment is already provisioned in `.venv` (Python 3.12 +
`piper-tts` + `jsonschema`). To rebuild it:

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e .
```

Then check everything the pipeline depends on:

```bash
.venv/bin/cf doctor
```

It verifies ffmpeg, the API key, the three prompt files, the piper voice (and
downloads it from HuggingFace on first run), the ComfyUI workflow, and ComfyUI's
reachability.

## Usage

```bash
# Test run: full chain, draft cut, review notes. This is the normal entry point.
.venv/bin/cf run "why electricity isn't a fuel"

# After watching output/draft_cut.mp4 and filling in NOTES.md:
.venv/bin/cf run "why electricity isn't a fuel" --mode full
```

Other flags:

```bash
--slug NAME        run directory name (default: slugified topic)
--from STAGE       start at a stage: research story script derive audio frames assemble
--force            re-run every stage instead of skipping existing artifacts
```

Every run gets its own directory, which is also the unit of resumption:

```
runs/<slug>/
  research/report.md          story/story.md
  script/base_script.json     script/frame_script.json   script/narration_script.json
  media/audio/*.wav           media/frames/*.png
  media/audio.json            media/frames.json          (measured durations, seeds)
  output/draft_cut.mp4        output/timings.json
  output/contact_sheet.png    output/pacing_report.md
  NOTES.md                    run.log
```

Typical iterations:

```bash
.venv/bin/cf run "<topic>" --from script --force   # rewrite after a prompt change
.venv/bin/cf run "<topic>" --from frames          # re-roll frames only
.venv/bin/cf run "<topic>" --from assemble        # re-cut only, no generation
```

## The review gate

`--mode test` renders `output/draft_cut.mp4` plus two review aids:

- **`output/contact_sheet.png`** — every frame in one image, for judging style
  drift at a glance.
- **`NOTES.md`** — prompts for what dragged, what drifted, what felt flat.

Those notes are the feedback signal. Read them, edit the matching
`prompts/<agent>.vN.md`, bump the version, add a line to `prompts/CHANGELOG.md`,
and re-run. Set `VERDICT: approve` in `NOTES.md` when the draft is good enough to
spend a full generation on.

## Configuration

Everything is environment variables, documented in `.env.example`. The ones worth
knowing:

| Variable | Default | Why you'd change it |
| --- | --- | --- |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI, `gpt-4o-mini` | Any OpenAI-compatible endpoint: OpenRouter, Groq, Ollama, vLLM. |
| `TARGET_DURATION_S` | `90` | Target video length. Drives the segment count the scriptwriter aims for. |
| `PIPER_VOICE` | `en_US-ryan-medium` | Voice. Auto-downloaded from HuggingFace on first use. |
| `PIPER_LENGTH_SCALE` | `1.0` | Speech rate. `>1` slower. |
| `COMFY_BASE_URL` | `auto` | `auto` probes localhost, the WSL2 gateway and the resolver. Set it if the probe guesses wrong. |
| `COMFY_WORKFLOW` | `workflows/stickfigure.json` | The ComfyUI graph to drive. |
| `COMFY_WIDTH` / `COMFY_HEIGHT` | `768` / `432` | Native generation size. 1080p is the *output* size; frames are upscaled by ffmpeg. |
| `SEGMENT_PADDING_S` | `0.25` | Silence after each clip. Prevents video/audio drift. |
| `CAPTION_MODE` | `auto` | `auto` = `on_screen_text` when set, else the narration. `on_screen_text`, or `off`. |
| `MEDIA_MAX_RETRIES` | `3` | Per-segment retries before the run fails. |

## ComfyUI from WSL2

ComfyUI runs on the Windows host and the pipeline drives it over HTTP. It must
listen on all interfaces, or WSL2 cannot reach it:

```bash
# on Windows, in the ComfyUI directory
python main.py --listen 0.0.0.0 --port 8188
```

Windows Firewall must allow inbound 8188. The alternative is WSL2 mirrored
networking (`networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`), after which
`127.0.0.1:8188` works and no firewall rule is needed.

Then export your graph: **Workflow → Export (API)**, save as
`workflows/stickfigure.json`, and put these tokens in the corresponding nodes:

| Token | Goes in |
| --- | --- |
| `{{POSITIVE}}` | positive CLIP text |
| `{{NEGATIVE}}` | negative CLIP text |
| `{{SEED}}`, `{{STEPS}}`, `{{CFG}}` | sampler |
| `{{WIDTH}}`, `{{HEIGHT}}` | empty latent image |
| `{{CHECKPOINT}}` | checkpoint loader |

The shipped `workflows/stickfigure.json` is a plain SD1.5 txt2img graph to start
from. Node ids are treated as data, so swapping in a different graph needs no
code change. Generated frames come back over `/view`, so nothing has to cross the
`\\wsl$` path boundary.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

29 tests, no network and no GPU. The end-to-end case builds real images and audio
with ffmpeg's synthetic sources, renders an actual MP4, and asserts the output
duration equals the timeline exactly. `tests/mock_services.py` impersonates both
OpenAI and ComfyUI, so the network clients are covered too:

```bash
.venv/bin/python tests/mock_services.py 8765
OPENAI_API_KEY=x OPENAI_BASE_URL=http://127.0.0.1:8765/v1 OPENAI_MODEL=x \
COMFY_BASE_URL=http://127.0.0.1:8765 COMFY_CHECKPOINT=x \
.venv/bin/cf run "any topic" --slug offline
```

## Layout

```
schema/base_script.schema.json   the contract everything validates against
prompts/                         versioned system prompts, one file per agent
workflows/stickfigure.json       ComfyUI graph with {{TOKEN}} placeholders
src/cf/
  script.py     schema validation + the deterministic derive step
  agents.py     the three LLM stages         llm.py   OpenAI-compatible client
  media.py      piper TTS, ComfyUI client, timing reconciliation
  assemble.py   ffmpeg: concat filter, fades, caption burn-in, contact sheet
  pipeline.py   CLI, stage resume, the human gate
  config.py     env -> settings               util.py  retries, subprocess, ffprobe
tests/                             test_pipeline.py + mock_services.py
runs/<slug>/                       every artifact, per run
```

## Known limits

- **Style drift is possible.** Consistency is prompt-only for now. If the contact
  sheet shows the characters changing between frames, the fix is reference-image
  injection or a fixed SVG style system, not a longer prompt.
- **Fades are dips to black**, not crossfades — the honest treatment for stills.
  Real blending needs one clip per segment and an `xfade` graph.
- **`hold` renders identically to `cut`**, and is kept distinct only in the
  manifest.
- **The research report is single-shot** (up to ~120k characters). A larger topic
  fails loudly rather than truncating; chunking is the fix if you hit it.
- **Frames are generated at 768×432 and upscaled**, a concession to a 6GB GPU. On
  a larger card, raise `COMFY_WIDTH`/`COMFY_HEIGHT`.
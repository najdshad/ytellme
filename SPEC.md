## System Overview and Data Flow

This spec defines a pipeline that produces fully automated explainer/essay videos — simple stick-figure graphics with a narrator — from a single topic input. The system is a chain of specialized agents whose intermediate artifacts are stored on disk, with a structured script as the single source of truth for everything downstream.

The flow:

`Topic → Researcher → Editor/Storywriter → Scriptwriter → (base script) → derived scripts → Voice TTS + Image Gen → FFmpeg assembler → final video`

Key design decisions locked in:

- **Audio drives everything.** The narration audio is the master clock; every frame's duration equals its audio segment's duration. The script carries only intent, never final timing.
    
- **The base script is a strict, machine-parseable schema**, not prose. All editing cues live in the script; the assembler is a thin, dumb wrapper around FFmpeg.
    
- **Visual consistency is handled by prompt discipline**, not by a consistency model or reference-image pipeline (for now).
    
- **Per-segment synthesis.** Narration is split into per-frame segments and each is synthesized separately — no forced alignment needed.
    
- **A human test run gates full generation.**
    
- **The prompt layer is an explicit, first-class component** whose optimization is part of the project's work.
    

## Script Schema (Core Artifact)

The base script is the only artifact downstream stages read. It must be a strict JSON (or structured YAML) document validated against a schema. Loose, prose-based scripts are rejected at validation time.

Each **segment** corresponds to one frame plus one narration chunk:

```json
{
  "segment_id": "s03",
  "narration": "But electricity isn't a fuel — it's a carrier.",
  "scene_intent": "Two stick figures on a hill; one points at power lines, sun in background.",
  "duration_hint_s": 4,
  "transition": "cut",
  "on_screen_text": null
}
```

Field semantics:

- `narration` — exactly what the TTS speaks. Written for the ear: short sentences, spoken register.
    
- `scene_intent` — the visual meaning of the frame, written for humans to read. This is _not_ the image-gen prompt; that is derived (see below).
    
- `duration_hint_s` — intent only. Actual duration is determined by the synthesized audio; the hint is advisory and used for pacing feedback, not assembly.
    
- `transition` — one of an enum (`cut`, `hold`, `fade`). Everything else is unsupported in v1.
    
- `on_screen_text` — optional caption; the assembler may burn it in.
    

The script also carries a document-level header: title, global style description, and the style-prompt block injected into every image generation call. The schema is versioned; the assembler rejects unknown versions.

Each stage writes its artifact to a run directory (`research/`, `story/`, `script/`, `media/`, `output/`), so the pipeline can resume from any stage.

## Agent Contracts: Researcher, Editor/Storywriter, Scriptwriter

Each agent has a defined input artifact, output artifact, and a system prompt. System prompts are versioned files in the repository — optimizing them is treated as real engineering work, not a one-time setup. The **prompt layer** is therefore explicit: every agent's system prompt lives in a dedicated `prompts/` directory with its own version and change log, and few-shot examples tuned to this specific format (short explainer, stick figure, narrator-driven).

### Researcher

- **Input:** topic (and any user constraints).
    
- **Job:** explore and investigate the topic broadly, and return its findings as a **big report of everything relevant** — facts, mechanisms, numbers, context, counterpoints, candidate angles, and sources. Deliberately expansive; the researcher should not pre-digest for story.
    
- **Output:** `research/report.md` — a structured research dossier.
    

### Editor / Storywriter

- **Input:** the researcher's report.
    
- **Job:** bring out a **compelling story to tell**. Selects the angle, decides what to include and, just as importantly, what to cut, and shapes narrative arc — hook, build, payoff. The editor does implicit research triage by reading the full report, but does not add new claims.
    
- **Output:** `story/story.md` — the narrative with a suggested beat structure.
    

### Scriptwriter

- **Input:** the story.
    
- **Job:** turn the story into the base script: narration lines, scene intents, transitions, and duration hints, conforming to the schema in the previous section.
    
- **Output:** `script/base_script.json` (schema-validated).
    

A deliberate division of labor: the researcher's report being large is a feature, not a bug — it gives the editor raw material. Quality pressure concentrates on the editor and scriptwriter prompts, which are expected to be iterated most.

## Derived Scripts: Frame Generation and Audio Narration

From the validated base script, a thin deterministic translation step (plain code, not an agent) generates specialized scripts for the two media stages:

### Frame script (for image generation)

For each segment: `segment_id`, the global style-prompt block from the header, and the image-gen prompt derived from `scene_intent`. The translation applies prompt-layer rules: the style block is always prepended verbatim, and scene intents are normalized to the image model's preferred register (subject, setting, composition; no text in image). Because consistency is handled **by the prompt** for now, this translation step is where consistency discipline is enforced mechanically — the style block must be byte-identical across all segments.

### Narration script (for TTS)

For each segment: `segment_id` and `narration` text, plus voice parameters (voice, speed, format). One synthesis call per segment; no whole-narration synthesis.

Rationale: the scriptwriter writes narration for the ear and scenes for human visualization; the image model needs a different register. Deriving both specialized scripts in code keeps the scriptwriter's contract simple and makes prompt-consistency rules enforceable in one place rather than trusting the agent to comply.

## Voice Synthesis and Image Generation Contracts

**Voice synthesis.** Input is the narration script, one segment per call. Output is `media/audio/s03.mp3` (or WAV) plus a sidecar with measured duration. On failure (corrupt or empty clip, synthesis error): retry up to N times, then mark the segment failed and stop that run at the checkpoint.

**Image generation.** Input is the frame script segments. Output is `media/frames/s03.png`, one per segment. The consistency strategy is **prompt-only for now**: the header's style block plus per-scene intent is the entire mechanism. If drift proves unacceptable in practice, options (SVG-based fixed style system, reference-image injection) are deferred decisions, not v1 scope. Failure handling uses the same retry-then-fail policy as audio.

**Timing reconciliation.** Assembly ignores `duration_hint_s` for timing. Frame duration = measured audio segment duration (plus a small configurable padding). A hint-vs-actual report can be generated for pacing feedback to the scriptwriter prompt, but it never feeds the assembler.

## Assembly Module (FFmpeg Wrapper)

The assembler is a light wrapper around FFmpeg (~150–300 lines of Python), with no creative logic.

- **Input:** the base script's segment list plus, per segment: `(image_path, audio_path, measured_duration, transition, on_screen_text)`.
    
- **Process:** per segment, turn the still image into a video clip held for the audio duration; concat audio; apply transitions from the enum; optionally burn in captions.
    
- **Output:** a single final video file, plus a log of per-segment timings.
    

The assembler validates the base script against the schema and the media manifest before running. It owns no naming, no timing decisions, and no content.

## Checkpoints, Test Run, and Error Handling

**Checkpoints and reruns.** Every stage writes its artifact to the run directory. The pipeline can be launched at any stage using the previous stage's artifacts, so an image failure never forces re-running research or story.

**Human test run before full generation.** A **test run** mode runs the full chain for one topic but stops after media generation and assembles only a draft cut. A human verifies the draft — narration quality, frame consistency, pacing, transitions — before **full generation** is approved. This gate is part of the standard flow, not an exception.

**Prompt optimization loop.** The test-run verdicts are the feedback signal for the prompt layer. Each test run can produce notes (what dragged, what drifted, what felt flat), which map to concrete prompt changes. System prompt changes are versioned, so prompt iterations are comparable across test runs. This optimization is explicitly part of the project's work.

**Error policy.** Per-segment retries (default 3) for TTS and image calls; hard fail with a readable report on schema validation failure or missing artifacts; and no silent fallbacks — a failed segment never gets a placeholder in the final video.

## Open Questions and Deferred Decisions

- **Caption rendering details** — font, position, burn-in vs. overlay timing — deferred until first test run.
    
- **Consistency escalation** — if prompt-only consistency fails visually, decide between SVG output and reference-image injection.
    
- **Image resolution and aspect ratio** — pin these in the schema header before the first test run (16:9, 1080p assumed for v1).
    
- **Researcher report size limits** — whether the editor needs chunking for very large reports; decide when observed in practice.
    
- **Music / silence padding** — out of v1 scope; the schema leaves room for an audio-mixing cue later.
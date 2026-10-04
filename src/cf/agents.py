"""The three LLM stages. Each one: read a versioned prompt, make one call, write one artifact.

All prompt content lives in prompts/*.v<N>.md. No prompt text in this file - the
prompts are the thing being iterated, so they have to be diffable on their own.
"""

import json
import re
from pathlib import Path

from .config import PROMPTS_DIR, Config
from .llm import chat
from .script import ScriptError, validate
from .util import CfError

PROMPT_VERSION = "v1"
AGENTS = ("researcher", "editor", "scriptwriter")


def prompt_path(agent: str, version: str = PROMPT_VERSION) -> Path:
    if agent not in AGENTS:
        raise CfError(f"unknown agent {agent!r}; expected one of {AGENTS}")
    path = PROMPTS_DIR / f"{agent}.{version}.md"
    if not path.exists():
        available = sorted(p.name for p in PROMPTS_DIR.glob(f"{agent}.*.md"))
        raise CfError(f"no prompt {version} for {agent} (found: {', '.join(available) or 'none'})")
    return path


def load_prompt(agent: str, subs: dict | None = None, version: str = PROMPT_VERSION) -> str:
    text = prompt_path(agent, version).read_text(encoding="utf-8")
    for key, value in (subs or {}).items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def segment_count(cfg: Config) -> int:
    # ~4.5s of narration per segment is what the v1 scriptwriter prompt targets.
    return max(6, round(cfg.target_duration_s / 4.5))


def research(cfg: Config, topic: str) -> str:
    prompt = load_prompt("researcher")
    user = f"Topic: {topic}\n\nResearch this topic and return the dossier."
    return chat(cfg, prompt, user, max_tokens=8000)


def edit(cfg: Config, topic: str, report: str) -> str:
    # ponytail: single-shot context. A dossier past ~120k chars fails in llm.py
    # rather than silently truncating; add chunk + map-reduce if you see it.
    if len(report) > 120_000:
        raise CfError(
            f"research report is {len(report):,} chars, past the 120k single-shot limit. "
            "Either tighten the researcher prompt or add chunking in agents.edit()."
        )
    prompt = load_prompt("editor", {"TARGET_DURATION_S": cfg.target_duration_s})
    user = f"Topic: {topic}\n\nHere is the research dossier:\n\n---\n{report}\n---\n\nReturn the story."
    return chat(cfg, prompt, user, max_tokens=6000)


def script(cfg: Config, story: str) -> dict:
    prompt = load_prompt(
        "scriptwriter",
        {"SEGMENT_COUNT": segment_count(cfg), "TARGET_DURATION_S": cfg.target_duration_s},
    )
    user = f"Here is the story:\n\n---\n{story}\n---\n\nReturn the base script JSON only."
    raw = chat(cfg, prompt, user, json_mode=True, max_tokens=8000)
    return validate(_extract_json(raw), source="scriptwriter output")


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        if start > 0:
            text = text[start:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        head = raw[:200].replace("\n", " ")
        raise ScriptError(
            f"scriptwriter did not return parseable JSON ({exc}). "
            f"Response started: {head!r}. A truncated response usually means max_tokens was too low."
        ) from None
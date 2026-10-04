"""One OpenAI-compatible /chat/completions client, stdlib only.

Works against OpenAI, OpenRouter, Groq, vLLM, Ollama or LM Studio by changing
OPENAI_BASE_URL and OPENAI_MODEL. No SDK, because the request shape is one POST.
"""

import json
import urllib.error
import urllib.request

from .config import Config, require_llm
from .util import CfError, retry


def _post(cfg: Config, system: str, user: str, json_mode: bool, max_tokens: int | None) -> dict:
    body: dict = {
        "model": cfg.openai_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.openai_temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens

    request = urllib.request.Request(
        f"{cfg.openai_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.openai_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.openai_timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise CfError(f"LLM HTTP {exc.code} from {cfg.openai_base_url}: {detail}") from None
    except urllib.error.URLError as exc:
        raise CfError(f"cannot reach {cfg.openai_base_url}: {exc.reason}") from None


def chat(cfg: Config, system: str, user: str, *, json_mode: bool = False, max_tokens: int | None = None) -> str:
    require_llm(cfg)

    def once() -> str:
        payload = retry(
            lambda: _post(cfg, system, user, json_mode, max_tokens),
            cfg.llm_max_retries,
            "llm",
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise CfError(f"unexpected response shape: {json.dumps(payload)[:400]}") from None
        if not isinstance(content, str) or not content.strip():
            raise CfError(f"model returned empty content: {json.dumps(payload)[:400]}")
        usage = payload.get("usage") or {}
        if usage:
            print(
                f"    tokens: {usage.get('total_tokens', '?')} "
                f"(prompt {usage.get('prompt_tokens', '?')}, completion {usage.get('completion_tokens', '?')})",
                flush=True,
            )
        return content

    return once()
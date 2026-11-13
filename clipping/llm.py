"""How the editor talks to Claude. Two backends:

* "subscription" (default when CLAUDE_CODE_OAUTH_TOKEN is set or the `claude` CLI is logged in):
  runs Claude Code in print mode (`claude -p`) with no tools, so usage comes out of the Claude Pro/Max
  subscription instead of API billing. Get the token with `claude setup-token`.
* "api": the Anthropic API with ANTHROPIC_API_KEY (billed per token).

Both return a dict validated against the step's JSON schema. Model defaults to Haiku for every step
(lighter on the Pro plan's usage limits); override per step with CLAUDE_MODEL_TRIAGE / _WRITE / _QC.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# CLI aliases -> API model ids
API_IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
           "fable": "claude-fable-5-2"}


class LLMError(RuntimeError):
    pass


def backend() -> str:
    forced = os.environ.get("EDITOR_BACKEND")
    if forced in ("subscription", "api"):
        return forced
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "subscription"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return "subscription"  # local machine where `claude` is already logged in


def model_for(step: str) -> str:
    return os.environ.get(f"CLAUDE_MODEL_{step.upper()}") or os.environ.get("CLAUDE_MODEL") or "haiku"


# ------------------------------------------------------------------ schema handling

def schema_instructions(schema: dict) -> str:
    return ("\n\nOUTPUT: reply with ONLY one JSON object that matches this JSON Schema — no prose, no markdown, "
            "no code fences. Include every required field.\n" + json.dumps(schema, ensure_ascii=False))


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("resposta sem JSON")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON inválido: {e}") from e


def validate(obj, schema: dict, path: str = "$") -> None:
    """Minimal validator for the schema subset used here (object/array/string/boolean/enum/required)."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(obj, dict):
            raise LLMError(f"{path}: esperado objeto")
        for k in schema.get("required", []):
            if k not in obj:
                raise LLMError(f"{path}.{k}: campo obrigatório ausente")
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                validate(obj[k], sub, f"{path}.{k}")
    elif t == "array":
        if not isinstance(obj, list):
            raise LLMError(f"{path}: esperado lista")
        for i, item in enumerate(obj):
            validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(obj, str):
            raise LLMError(f"{path}: esperado texto")
        if "enum" in schema and obj not in schema["enum"]:
            raise LLMError(f"{path}: valor fora da lista ({obj})")
    elif t == "boolean" and not isinstance(obj, bool):
        raise LLMError(f"{path}: esperado true/false")


# ------------------------------------------------------------------ backends

def _claude_cli() -> str:
    exe = os.environ.get("CLAUDE_CLI") or shutil.which("claude")
    if not exe:
        raise LLMError("Claude Code (comando `claude`) não está instalado neste computador/servidor")
    return exe


def _via_subscription(system: str, prompt: str, model: str, effort: str, timeout: int) -> str:
    """`claude -p` with tools disabled and our own system prompt; the long input goes through stdin
    (command lines are too short for it, especially on Windows)."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # an API key would take precedence over the subscription token
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(system)
        sys_file = Path(f.name)
    cmd = [_claude_cli(), "-p", "Follow the instructions in the input exactly and answer with the JSON only.",
           "--output-format", "json", "--model", model, "--effort", effort, "--tools", "", "--max-turns", "3",
           "--no-session-persistence", "--system-prompt-file", str(sys_file)]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, env=env, cwd=tempfile.gettempdir())  # no project files loaded
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"Claude Code demorou mais de {timeout}s") from e
    finally:
        sys_file.unlink(missing_ok=True)
    out = proc.stdout.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise LLMError(f"Claude Code falhou (código {proc.returncode}): {(proc.stderr or out)[:400]}")
    if data.get("is_error") or data.get("subtype", "success") != "success":
        raise LLMError(f"Claude Code retornou erro: {str(data.get('result') or data)[:400]}")
    return data.get("result", "")


def _via_api(system: str, prompt: str, model: str, effort: str, schema: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic(max_retries=4)
    model_id = API_IDS.get(model, model)
    kwargs = {}
    if "haiku" not in model_id:  # Haiku has no server-side refusal fallback
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        with client.beta.messages.stream(
            model=model_id, max_tokens=64000, thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}], **kwargs,
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Claude API connection error: {e}") from e
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise LLMError(f"Claude stop_reason={msg.stop_reason}")
    return json.loads(next((b.text for b in msg.content if b.type == "text"), ""))


def complete(system: str, prompt: str, schema: dict, step: str, effort: str = "medium",
             timeout: int = 900) -> dict:
    model = model_for(step)
    if backend() == "api":
        out = _via_api(system, prompt, model, effort, schema)
        validate(out, schema)
        return out
    full = prompt + schema_instructions(schema)
    last: Exception | None = None
    for attempt in range(2):  # one retry with the validation error spelled out
        text = _via_subscription(system, full, model, effort, timeout)
        try:
            out = extract_json(text)
            validate(out, schema)
            return out
        except LLMError as e:
            last = e
            log.warning("Resposta do Claude (%s) inválida: %s — tentando de novo", step, e)
            full = prompt + schema_instructions(schema) + f"\n\nYour previous answer was invalid ({e}). Fix it."
    raise LLMError(str(last))

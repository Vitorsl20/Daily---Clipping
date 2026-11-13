import json
import os
import stat
import sys

import pytest

from clipping import llm

SCHEMA = {"type": "object", "required": ["items"], "additionalProperties": False, "properties": {
    "items": {"type": "array", "items": {"type": "object", "required": ["id", "keep"], "additionalProperties": False,
              "properties": {"id": {"type": "string"}, "keep": {"type": "boolean"}}}}}}

FAKE = r'''#!{py}
import json, os, sys
log = os.environ["FAKE_LOG"]
calls = json.load(open(log)) if os.path.exists(log) else []
stdin = sys.stdin.read()
calls.append({{"argv": sys.argv[1:], "stdin": stdin, "has_api_key": "ANTHROPIC_API_KEY" in os.environ}})
json.dump(calls, open(log, "w"))
answer = '```json\n{{"items": [{{"id": "a"}}]}}\n```' if len(calls) == 1 else '{{"items": [{{"id": "a", "keep": true}}]}}'
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": answer}}))
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE.format(py=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_CLI", str(script))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.json"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.delenv("EDITOR_BACKEND", raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    return tmp_path / "calls.json"


@pytest.mark.skipif(os.name == "nt", reason="shebang script")
def test_subscription_backend_retries_invalid_output(fake_cli):
    out = llm.complete("RULES", "Task <data>x</data>", SCHEMA, "triage", "low")
    assert out == {"items": [{"id": "a", "keep": True}]}
    calls = json.load(open(fake_cli))
    assert len(calls) == 2                                   # first answer missed a required field
    argv = calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku" and argv[argv.index("--tools") + 1] == ""
    assert "--system-prompt-file" in argv and "--effort" in argv
    assert "Task <data>x</data>" in calls[0]["stdin"] and "JSON Schema" in calls[0]["stdin"]
    assert not calls[0]["has_api_key"]                       # subscription token wins, API key stripped
    assert "previous answer was invalid" in calls[1]["stdin"]


def test_backend_selection(monkeypatch):
    monkeypatch.delenv("EDITOR_BACKEND", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert llm.backend() == "subscription"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert llm.backend() == "api"
    monkeypatch.setenv("CLAUDE_MODEL_WRITE", "sonnet")
    assert llm.model_for("write") == "sonnet" and llm.model_for("triage") == "haiku"

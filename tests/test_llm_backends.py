"""Backends, exercised without any real model:

* `claude -p` through a fake `claude` executable (same subprocess path as the real CLI),
* a local OpenAI-compatible server stubbed with http.server on localhost,
* scripted / transcript / replay backends.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from nfv.llm import (ClaudeCLIBackend, LLMError, OpenAICompatBackend, ReplayBackend, ScriptedBackend, Transcript,
                     make_backend, stage_of)

HERE = Path(__file__).resolve().parent


def fake_claude(tmp_path: Path, scenarios: list[str] = ()) -> tuple[str, dict]:
    """A `claude` executable (shell wrapper around fake_claude.py) and the env it needs."""
    exe = tmp_path / "claude"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_claude.py"}" "$@"\n')
    exe.chmod(0o755)
    env = {"FAKE_CLAUDE_LOG": str(tmp_path / "argv.jsonl"), "FAKE_CLAUDE_STATE": str(tmp_path / "state.json"),
           "FAKE_CLAUDE_SCENARIOS": ",".join(str(HERE / "fixtures" / "llm" / s) for s in scenarios)}
    return str(exe), env


def test_claude_cli_backend_flags_stdin_and_json(tmp_path, monkeypatch):
    exe, env = fake_claude(tmp_path, ["shifted_mean"])
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    b = ClaudeCLIBackend(model="sonnet", executable=exe, effort="medium")
    c = b.complete("[NFV-STAGE: dafny.theory]\nformalize sum", system="be terse")
    assert c.stage == "dafny.theory"
    assert "function Sum" in c.text
    assert c.meta["is_error"] is False and c.meta["session_id"] == "fake"
    argv = json.loads((tmp_path / "argv.jsonl").read_text().splitlines()[0])["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--tools") + 1] == ""          # built-in tools disabled
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__*"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--system-prompt") + 1] == "be terse"
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--effort") + 1] == "medium"
    # isolated from the user's setup and from this repository
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    cwd = json.loads((tmp_path / "argv.jsonl").read_text().splitlines()[0])["cwd"]
    assert "nfv-claude-" in cwd and not Path(cwd).exists()  # fresh empty dir, removed afterwards
    assert json.loads((tmp_path / "argv.jsonl").read_text().splitlines()[0])["files_in_cwd"] == []
    # the prompt travelled on stdin, not argv
    assert not any("formalize sum" in a for a in argv)


def test_claude_cli_backend_error_and_overrides(tmp_path, monkeypatch):
    exe, env = fake_claude(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    with pytest.raises(LLMError, match="simulated failure"):
        ClaudeCLIBackend(executable=exe, retries=1).complete("[NFV-STAGE: x]\nhi")
    monkeypatch.delenv("FAKE_CLAUDE_FAIL")
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_OUT", "1")
    with pytest.raises(LLMError, match="skips OAuth"):
        ClaudeCLIBackend(executable=exe, isolation="bare").complete("[NFV-STAGE: x]\nhi")
    monkeypatch.setenv("NFV_CLAUDE_ARGS", "--output-format json --model opus")
    assert ClaudeCLIBackend(executable=exe).command(None) == [exe, "-p", "--output-format", "json", "--model", "opus"]
    monkeypatch.delenv("NFV_CLAUDE_ARGS")
    assert "--bare" in ClaudeCLIBackend(executable=exe, isolation="bare").args
    with pytest.raises(ValueError):
        ClaudeCLIBackend(executable=exe, isolation="leaky")
    with pytest.raises(LLMError, match="cannot run"):
        ClaudeCLIBackend(executable=str(tmp_path / "missing")).complete("hi")


def test_rate_limit_events_feed_the_usage_monitor():
    from nfv.llm import UsageBudgetExceeded, UsageMonitor
    b = ClaudeCLIBackend(executable="claude")
    stream = "\n".join([json.dumps({"type": "system"}),
                        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
                            "five_hour": {"utilization": 0.42, "resetsAt": 1}, "seven_day": {"utilization": 0.1}}}}),
                        json.dumps({"type": "result", "result": "ok", "is_error": False})])
    text, meta = b.parse_output(stream)
    m = UsageMonitor(soft=0.4, hard=0.5)
    m.update(meta["rate_limit"])
    assert text == "ok" and m.current == 0.42 and m.soft_reached()
    m.check()  # below hard
    m.update({"unifiedWindows": {"five_hour": {"utilization": 0.51}}})
    with pytest.raises(UsageBudgetExceeded):
        m.check()
    assert "five_hour 51%" in m.describe()


def test_claude_output_parsing_variants():
    b = ClaudeCLIBackend(executable="claude")
    assert b.parse_output("plain text answer") == ("plain text answer", {})
    stream = "\n".join([json.dumps({"type": "system"}), json.dumps({"type": "assistant", "message": {}}),
                        json.dumps({"type": "result", "result": "final", "is_error": False})])
    assert b.parse_output(stream)[0] == "final"


class _Handler(BaseHTTPRequestHandler):
    requests: list = []
    fail_first = 0

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.requests.append((self.path, dict(self.headers), body))
        if _Handler.fail_first > 0:
            _Handler.fail_first -= 1
            self.send_response(500)
            self.end_headers()
            return
        text = "<think>let me reason about Sum</think>\n```dafny\nfunction Sum(l: seq<real>): real { 0.0 }\n```"
        payload = {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"total_tokens": 7}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    _Handler.requests = []
    _Handler.fail_first = 0
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_openai_compatible_local_backend(local_server):
    b = OpenAICompatBackend(base_url=local_server, model="qwen-coder", temperature=0.0, retries=2)
    _Handler.fail_first = 1  # first request 500s -> retried
    c = b.complete("[NFV-STAGE: dafny.theory]\nhello", system="sys")
    assert c.stage == "dafny.theory"
    assert "<think>" not in c.text and c.text.startswith("```dafny")
    assert c.meta["usage"]["total_tokens"] == 7
    path, headers, body = _Handler.requests[-1]
    assert path == "/v1/chat/completions"
    assert body["model"] == "qwen-coder" and body["temperature"] == 0.0 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "[NFV-STAGE: dafny.theory]\nhello"}]
    assert len(_Handler.requests) == 2


def test_local_backend_unreachable_raises():
    b = OpenAICompatBackend(base_url="http://127.0.0.1:9/v1", retries=0, timeout=2)
    with pytest.raises(LLMError):
        b.complete("hi")


def test_factory():
    assert isinstance(make_backend("claude", model="opus"), ClaudeCLIBackend)
    b = make_backend("ollama", model="qwen2.5-coder")
    assert isinstance(b, OpenAICompatBackend) and b.base_url == "http://localhost:11434/v1"
    assert make_backend("local", base_url="http://h:1/v1").base_url == "http://h:1/v1"
    with pytest.raises(ValueError):
        make_backend("gpt-telepathy")


def test_scripted_transcript_and_replay(tmp_path):
    s = ScriptedBackend({"a": ["one", "two"], "b": ["only"]})
    t = Transcript(s, tmp_path / "t.jsonl", {"run": "x"}).with_context(entry_id="E1")
    assert t.complete("[NFV-STAGE: a]\np1").text == "one"
    assert t.complete("[NFV-STAGE: a]\np2").text == "two"
    assert t.complete("[NFV-STAGE: a]\np3").text == "two"   # last response repeats
    assert t.complete("[NFV-STAGE: b]\nq").text == "only"
    recs = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [r["stage"] for r in recs] == ["a", "a", "a", "b"] and recs[0]["entry_id"] == "E1" and recs[0]["run"] == "x"
    r = ReplayBackend(tmp_path / "t.jsonl")
    assert r.complete("[NFV-STAGE: a]\np2").text == "two"
    with pytest.raises(LLMError):
        r.complete("never seen")
    assert stage_of("no header") == "unknown"

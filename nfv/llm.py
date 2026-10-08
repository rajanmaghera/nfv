"""LLM front ends.

Every pipeline stage is a single stateless prompt -> completion call; repair
rounds re-send the previous artifact and the checker's feedback in a fresh
prompt. That keeps every backend interchangeable:

* ``ClaudeCLIBackend``   -- shells out to ``claude -p`` (prompt on stdin,
                            ``--output-format json``), tools disabled.
* ``OpenAICompatBackend`` -- any OpenAI-compatible ``/chat/completions``
                            endpoint: llama.cpp ``llama-server``, Ollama
                            (``/v1``), vLLM, LM Studio, ...
* ``ScriptedBackend``    -- canned responses, for tests and dry runs.
* ``ReplayBackend``      -- replays a transcript recorded by ``Transcript``.

Every prompt starts with a ``[NFV-STAGE: <name>]`` header so scripted/replay
backends (and humans reading transcripts) can tell the stages apart.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

STAGE_RE = re.compile(r"^\[NFV-STAGE: ([\w.-]+)\]", re.M)


class LLMError(RuntimeError):
    pass


def stage_of(prompt: str) -> str:
    m = STAGE_RE.search(prompt)
    return m.group(1) if m else "unknown"


@dataclass
class Completion:
    text: str
    stage: str
    seconds: float = 0.0
    meta: dict = field(default_factory=dict)


class Backend:
    name = "backend"

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        raise NotImplementedError


# ---------------------------------------------------------------- claude -p


class ClaudeCLIBackend(Backend):
    """`claude -p` in print mode. The prompt goes on stdin to avoid argv limits.

    Claude is isolated from the repository and the user's setup:

    * every call runs in a fresh, empty temporary working directory (nothing to
      read, no project CLAUDE.md, no git repo);
    * ``--tools ""`` and ``--disallowedTools mcp__*``: no tools, so the model answers
      from the prompt like every other backend (the harness runs the verifier);
    * isolation mode ``safe-mode`` (default): ``--safe-mode --strict-mcp-config
      --setting-sources ""`` -- no CLAUDE.md, memory, skills, plugins, hooks or MCP
      servers, but normal (OAuth) login still works. ``bare`` uses ``--bare`` instead,
      which skips OAuth too and so needs ``ANTHROPIC_API_KEY``;
    * ``--no-session-persistence`` and ``--output-format json``.

    ``extra_args`` / ``NFV_CLAUDE_ARGS`` replace the default flags entirely.
    """

    name = "claude-cli"
    BASE_ARGS = ["--output-format", "json", "--tools", "", "--disallowedTools", "mcp__*", "--no-session-persistence"]
    ISOLATION_ARGS = {
        "safe-mode": ["--safe-mode", "--strict-mcp-config", "--setting-sources", ""],
        "bare": ["--bare", "--strict-mcp-config"],
        "none": [],
    }
    DEFAULT_ARGS = BASE_ARGS + ISOLATION_ARGS["safe-mode"]

    def __init__(self, model: str | None = None, executable: str | None = None,
                 extra_args: list[str] | None = None, timeout: float = 900, retries: int = 2,
                 effort: str | None = None, isolation: str | None = None):
        self.model = model
        self.effort = effort or os.environ.get("NFV_CLAUDE_EFFORT")
        self.executable = executable or os.environ.get("NFV_CLAUDE_BIN", "claude")
        isolation = isolation or os.environ.get("NFV_CLAUDE_ISOLATION", "safe-mode")
        if isolation not in self.ISOLATION_ARGS:
            raise ValueError(f"isolation must be one of {sorted(self.ISOLATION_ARGS)}")
        env_args = os.environ.get("NFV_CLAUDE_ARGS")
        if extra_args is not None:
            self.args = list(extra_args)
        elif env_args is not None:
            self.args = shlex.split(env_args)
        else:
            self.args = self.BASE_ARGS + self.ISOLATION_ARGS[isolation]
        self.timeout = timeout
        self.retries = retries

    def command(self, system: str | None) -> list[str]:
        cmd = [self.executable, "-p"] + self.args
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if system:
            cmd += ["--system-prompt", system]
        return cmd

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        cmd = self.command(system)
        last_err = ""
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="nfv-claude-") as cwd:
                    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                          timeout=self.timeout, cwd=cwd)
            except subprocess.TimeoutExpired:
                last_err = f"timeout after {self.timeout}s"
                continue
            except FileNotFoundError as e:
                raise LLMError(f"cannot run {self.executable!r}: {e}") from e
            dt = time.monotonic() - t0
            if proc.returncode != 0 and not proc.stdout.strip():
                last_err = f"exit {proc.returncode}: {proc.stderr.strip()[-1000:]}"
                time.sleep(2 ** attempt)
                continue
            text, meta = self.parse_output(proc.stdout)
            if meta.get("is_error"):
                last_err = f"claude reported error: {text[:500]}"
                if "not logged in" in text.lower() or "/login" in text:
                    raise LLMError(last_err + " (--bare skips OAuth: use isolation 'safe-mode' or set ANTHROPIC_API_KEY)")
                time.sleep(2 ** attempt)
                continue
            return Completion(text=text, stage=stage_of(prompt), seconds=dt, meta=meta)
        raise LLMError(last_err)

    def parse_output(self, stdout: str) -> tuple[str, dict]:
        """Accepts `--output-format json` (a result object), `stream-json`
        (last result event wins) or plain text."""
        s = stdout.strip()
        if not s.startswith("{"):
            return s, {}
        objs = []
        try:
            objs = [json.loads(s)]
        except json.JSONDecodeError:
            for line in s.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        objs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        results = [o for o in objs if isinstance(o, dict) and ("result" in o or o.get("type") == "result")]
        if not results:
            return s, {}
        r = results[-1]
        meta = {k: r.get(k) for k in ("is_error", "subtype", "total_cost_usd", "num_turns", "session_id", "usage",
                                      "modelUsage", "duration_ms") if k in r}
        return str(r.get("result", "")), meta


# ---------------------------------------------------------------- local LLM


class OpenAICompatBackend(Backend):
    """POST {base_url}/chat/completions. Works with llama.cpp's llama-server
    (default http://localhost:8080/v1), Ollama (http://localhost:11434/v1), vLLM."""

    name = "openai-compat"

    def __init__(self, base_url: str = "http://localhost:8080/v1", model: str = "local",
                 api_key: str | None = None, temperature: float = 0.2, max_tokens: int = 8192,
                 timeout: float = 1800, retries: int = 3, extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("NFV_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        self.extra_body = extra_body or {}

    def request_body(self, prompt: str, system: str | None) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": self.max_tokens, "stream": False}
        body.update(self.extra_body)
        return body

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        body = json.dumps(self.request_body(prompt, system)).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_err = ""
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers=headers)
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode())
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(min(2 ** attempt, 30))
                continue
            text = self.parse_response(payload)
            return Completion(text=text, stage=stage_of(prompt), seconds=time.monotonic() - t0,
                              meta={"usage": payload.get("usage")})
        raise LLMError(f"local LLM request failed: {last_err}")

    @staticmethod
    def parse_response(payload: dict) -> str:
        try:
            msg = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response: {str(payload)[:500]}") from e
        text = msg.get("content") or ""
        # reasoning models served by llama.cpp/vLLM may inline their thinking
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


# ---------------------------------------------------------------- testing


class ScriptedBackend(Backend):
    """Returns canned responses.

    ``responses`` is either a list (consumed in order) or a dict mapping stage
    name -> list of responses (consumed in order per stage). A callable gets
    the prompt and returns the text.
    """

    name = "scripted"

    def __init__(self, responses: list[str] | dict[str, list[str]] | Callable[[str], str]):
        self._lock = threading.Lock()
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        if isinstance(responses, dict):
            self._queues = {k: list(v) for k, v in responses.items()}
        elif isinstance(responses, list):
            self._queue = list(responses)

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        stage = stage_of(prompt)
        with self._lock:
            self.calls.append((stage, prompt))
            if callable(self.responses):
                text = self.responses(prompt)
            elif isinstance(self.responses, dict):
                q = self._queues.get(stage)
                if not q:
                    raise LLMError(f"scripted backend has no response left for stage {stage!r}")
                text = q.pop(0) if len(q) > 1 else q[0]
            else:
                if not self._queue:
                    raise LLMError("scripted backend exhausted")
                text = self._queue.pop(0)
        return Completion(text=text, stage=stage)


class ReplayBackend(Backend):
    """Replays responses from a transcript JSONL, matched by exact prompt."""

    name = "replay"

    def __init__(self, path: Path | str):
        self.by_prompt: dict[str, list[str]] = {}
        for rec in read_transcript(path):
            self.by_prompt.setdefault(rec["prompt"], []).append(rec["response"])

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        q = self.by_prompt.get(prompt)
        if not q:
            raise LLMError("prompt not found in transcript")
        return Completion(text=q.pop(0) if len(q) > 1 else q[0], stage=stage_of(prompt))


# ---------------------------------------------------------------- recording


class Transcript(Backend):
    """Wraps a backend and appends every call to a JSONL file."""

    def __init__(self, inner: Backend, path: Path | str, context: dict | None = None):
        self.inner = inner
        self.name = inner.name
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.context = context or {}
        self._lock = threading.Lock()

    def with_context(self, **ctx) -> "Transcript":
        t = Transcript.__new__(Transcript)
        t.inner, t.name, t.path, t._lock = self.inner, self.name, self.path, self._lock
        t.context = {**self.context, **ctx}
        return t

    def complete(self, prompt: str, *, system: str | None = None) -> Completion:
        c = self.inner.complete(prompt, system=system)
        rec = {**self.context, "stage": c.stage, "seconds": round(c.seconds, 3), "prompt": prompt,
               "system": system, "response": c.text, "meta": c.meta}
        with self._lock, open(self.path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return c


def read_transcript(path: Path | str) -> Iterable[dict]:
    with open(path) as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# ---------------------------------------------------------------- factory


def make_backend(kind: str, *, model: str | None = None, base_url: str | None = None,
                 claude_args: str | None = None, temperature: float = 0.2,
                 max_tokens: int = 8192, timeout: float | None = None, effort: str | None = None,
                 isolation: str | None = None) -> Backend:
    if kind in ("claude", "claude-cli"):
        return ClaudeCLIBackend(model=model, extra_args=shlex.split(claude_args) if claude_args is not None else None,
                                timeout=timeout or 900, effort=effort, isolation=isolation)
    if kind in ("local", "openai", "llama.cpp", "ollama", "vllm"):
        default_url = {"ollama": "http://localhost:11434/v1"}.get(kind, "http://localhost:8080/v1")
        return OpenAICompatBackend(base_url=base_url or os.environ.get("NFV_BASE_URL", default_url),
                                   model=model or os.environ.get("NFV_MODEL", "local"),
                                   temperature=temperature, max_tokens=max_tokens, timeout=timeout or 1800)
    if kind.startswith("replay:"):
        return ReplayBackend(kind.split(":", 1)[1])
    raise ValueError(f"unknown backend {kind!r} (claude | local | ollama | vllm | replay:<file>)")

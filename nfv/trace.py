"""Optional full-fidelity trace of a run: every LLM call, every verifier run and every pipeline event,
in order, with timestamps (``nfv run --trace`` -> ``<out>/trace.jsonl``). Used to build demos/replays."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_ctx = threading.local()


class Tracer:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, kind: str, **data):
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "t": round(time.time(), 3), "kind": kind, **context(), **data}
            with open(self.path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")


TRACER: Tracer | None = None


def enable(path: Path | str) -> Tracer:
    global TRACER
    TRACER = Tracer(path)
    return TRACER


def emit(kind: str, **data):
    if TRACER is not None:
        TRACER.emit(kind, **data)


def set_context(**kv):
    for k, v in kv.items():
        setattr(_ctx, k, v)


def context() -> dict:
    return {k: getattr(_ctx, k) for k in ("entry_id", "arm", "attempt") if getattr(_ctx, k, None) is not None}

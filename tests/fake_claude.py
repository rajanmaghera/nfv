"""A stand-in for the `claude` CLI, used by the smoke tests (never the real thing).

Behaves like `claude -p --output-format json`: reads the prompt from stdin and
prints a result object. Responses come from the sample LLM outputs in
``$FAKE_CLAUDE_SCENARIOS`` (comma-separated fixture directories), chosen by the
prompt's ``[NFV-STAGE: ...]`` header; a per-stage counter lives in
``$FAKE_CLAUDE_STATE``. Every invocation's argv is appended to
``$FAKE_CLAUDE_LOG``.

The judge stage answers from the prompt itself: INCORRECT iff it contains
``x - y`` (HumanEval/53's mutant).
"""

import json
import os
import re
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps({"argv": sys.argv[1:], "prompt_head": prompt[:80]}) + "\n")
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "simulated failure"}))
        return 1
    m = re.search(r"^\[NFV-STAGE: ([\w.-]+)\]", prompt, re.M)
    stage = m.group(1) if m else "unknown"
    if stage == "judge":
        text = "Subtraction is not addition.\nVERDICT: INCORRECT" if "x - y" in prompt else "VERDICT: CORRECT"
    else:
        state_path = Path(os.environ["FAKE_CLAUDE_STATE"])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        files = []
        for d in os.environ["FAKE_CLAUDE_SCENARIOS"].split(","):
            files += sorted(Path(d).glob(f"{stage}.*.md"), key=lambda p: int(p.name.rsplit(".", 2)[1]))
        if not files:
            print(json.dumps({"type": "result", "is_error": True, "result": f"no fixture for {stage}"}))
            return 1
        k = state.get(stage, 0)
        text = files[min(k, len(files) - 1)].read_text()
        state[stage] = k + 1
        state_path.write_text(json.dumps(state))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "total_cost_usd": 0.0, "num_turns": 1, "session_id": "fake"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

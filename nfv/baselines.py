"""Baselines of paper Section 4.5.

* LLM-as-judge: the model sees program, postcondition and docstring and answers
  CORRECT / INCORRECT. It always commits (no abstention, no artifact).
* Unstaged agent-verifier: one prompt asking for a verifying Dafny method that
  carries the postcondition, repaired against the verifier with no stages, no
  freeze and no audits. Its only possible verdict is ``verified``.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import prompts as P
from .dafny_pipeline import ABSTAIN, REFUTED, VERIFIED, ArmResult, Budget
from .dataset import Entry
from .extract import extract_code, extract_verdict
from .llm import Backend, LLMError
from .tools import Dafny


def llm_judge(entry: Entry, llm: Backend, retries: int = 2) -> ArmResult:
    res = ArmResult(entry_id=entry.id, arm="judge", label=entry.label)
    t0 = time.monotonic()
    prompt = P.judge(entry)
    for k in range(retries + 1):
        res.attempts_used = k + 1
        try:
            res.llm_calls += 1
            text = llm.complete(prompt, system=P.SYSTEM).text
        except LLMError as e:
            res.failure = "llm-error"
            res.events.append({"attempt": k + 1, "stage": "judge", "status": "error", "detail": str(e)})
            continue
        v = extract_verdict(text)
        res.events.append({"attempt": k + 1, "stage": "judge", "status": v or "unparsed", "detail": text[-1500:]})
        if v:
            res.verdict = VERIFIED if v == "CORRECT" else REFUTED
            res.failure = ""
            break
        res.failure = "unparsed"
    res.seconds = round(time.monotonic() - t0, 2)
    return res


def unstaged(entry: Entry, llm: Backend, dafny: Dafny, budget: Budget | None = None,
             artifacts_dir: Path | None = None) -> ArmResult:
    budget = budget or Budget()
    res = ArmResult(entry_id=entry.id, arm="unstaged", label=entry.label)
    t0 = time.monotonic()
    base = P.unstaged(entry)
    for attempt in range(1, budget.attempts + 1):
        res.attempts_used = attempt
        code, feedback = None, ""
        try:
            for rnd in range(budget.rounds + 1):
                prompt = base if code is None and rnd == 0 else base + P.repair_suffix(code or "", feedback)
                res.llm_calls += 1
                new = extract_code(llm.complete(prompt, system=P.SYSTEM).text, ("dafny",))
                if new is None:
                    feedback = "No fenced dafny block found."
                    continue
                code = new
                r = dafny.verify(code)
                if r.ok and not r.error_diags and r.verified > 0:
                    res.verdict, res.artifact, res.reached_proof = VERIFIED, code, True
                    res.events.append({"attempt": attempt, "stage": "unstaged", "status": "verified", "round": rnd})
                    if artifacts_dir:
                        artifacts_dir.mkdir(parents=True, exist_ok=True)
                        (artifacts_dir / "unstaged_proved.dfy").write_text(code)
                    res.seconds = round(time.monotonic() - t0, 2)
                    return res
                feedback = r.feedback()
                res.events.append({"attempt": attempt, "stage": "unstaged", "status": "failed", "round": rnd,
                                   "detail": feedback[-1500:]})
        except LLMError as e:
            res.failures.append("llm-error")
            res.events.append({"attempt": attempt, "stage": "llm", "status": "error", "detail": str(e)})
            continue
        res.failures.append("proof")
    res.verdict = ABSTAIN
    res.failure = res.failures[-1] if res.failures else ""
    res.seconds = round(time.monotonic() - t0, 2)
    return res

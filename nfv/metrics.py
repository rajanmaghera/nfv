"""Scoring (paper Section 4.1).

precision = correct verdicts / verdicts issued        (abstention is not a verdict)
recall    = correct verdicts / entries of the class the task targets
F1        = harmonic mean

* verification task: verdict `verified` is correct iff the entry is labeled correct;
  recall is over the correct entries (103 in the full corpus).
* refutation task (Dafny or CBMC): `refuted` is correct iff labeled incorrect;
  recall over the incorrect entries.
* pipeline: refutation first, verification on what it leaves open; every entry
  is in the population.
* judge: both verdicts, population = all entries, never abstains.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .dataset import CORRECT, INCORRECT, Entry

VERIFIED, REFUTED, ABSTAIN = "verified", "refuted", "abstain"


@dataclass
class Score:
    name: str
    population: int
    issued: int
    wrong: int

    @property
    def correct(self) -> int:
        return self.issued - self.wrong

    @property
    def precision(self) -> float:
        return self.correct / self.issued if self.issued else 0.0

    @property
    def recall(self) -> float:
        return self.correct / self.population if self.population else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def row(self) -> list[str]:
        return [self.name, str(self.population), str(self.issued), str(self.wrong),
                f"{100 * self.precision:.1f}", f"{100 * self.recall:.1f}", f"{self.f1:.2f}"]


def is_right(verdict: str, label: str) -> bool:
    return (verdict == VERIFIED and label == CORRECT) or (verdict == REFUTED and label == INCORRECT)


def score(name: str, results: list[dict], target: str | None) -> Score:
    """``target`` = 'verified' (verification task), 'refuted' (refutation task) or None (both)."""
    if target == VERIFIED:
        pop = sum(r["label"] == CORRECT for r in results)
        issued = [r for r in results if r["verdict"] == VERIFIED]
    elif target == REFUTED:
        pop = sum(r["label"] == INCORRECT for r in results)
        issued = [r for r in results if r["verdict"] == REFUTED]
    else:
        pop = len(results)
        issued = [r for r in results if r["verdict"] in (VERIFIED, REFUTED)]
    wrong = sum(not is_right(r["verdict"], r["label"]) for r in issued)
    return Score(name, pop, len(issued), wrong)


def compose_pipeline(refute: list[dict], verify: list[dict]) -> list[dict]:
    """Refute first; fall back to the verification verdict where refutation abstained."""
    v_by = {r["entry_id"]: r for r in verify}
    out = []
    for r in refute:
        if r["verdict"] == REFUTED:
            out.append({**r, "arm": "pipeline"})
        elif r["entry_id"] in v_by:
            out.append({**v_by[r["entry_id"]], "arm": "pipeline"})
        else:
            out.append({**r, "arm": "pipeline", "verdict": ABSTAIN})
    return out


def load_results(path: Path | str) -> list[dict]:
    p = Path(path)
    if p.is_dir():
        p = p / "results.jsonl"
    out = []
    with open(p) as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def by_arm(results: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        d[r["arm"]].append(r)
    return d


def table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows)]
    fmt = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths))  # noqa: E731
    return "\n".join([fmt(header), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows])


ARM_TARGET = {"verify": VERIFIED, "refute": REFUTED, "cbmc": REFUTED, "unstaged": VERIFIED, "judge": None,
              "pipeline": None}


def main_table(results: list[dict]) -> str:
    """Paper Table 3 layout, for whatever arms are present."""
    arms = by_arm(results)
    rows = []
    for arm in ("verify", "refute", "cbmc", "unstaged", "judge"):
        if arm in arms:
            rows.append(score(arm, arms[arm], ARM_TARGET[arm]).row())
    if "refute" in arms and "verify" in arms:
        rows.append(score("pipeline (refute, then verify)", compose_pipeline(arms["refute"], arms["verify"]),
                          None).row())
    return table(rows, ["task", "population", "issued", "wrong", "prec. (%)", "rec. (%)", "F1"])


def tier_table(results: list[dict], entries: list[Entry]) -> str:
    """Paper Table 4 layout."""
    tier_of = {e.id: e.tier for e in entries}
    arms = by_arm(results)
    rows = []
    for tier in ("L0", "L1", "L2a", "L2b", "L3", "All"):
        row = [tier]
        for arm, target in (("verify", VERIFIED), ("refute", REFUTED)):
            rs = [r for r in arms.get(arm, []) if tier == "All" or tier_of.get(r["entry_id"]) == tier]
            s = score(arm, rs, target)
            row += [f"{s.correct}/{s.population} ({100 * s.recall:.0f}%)" if s.population else "-",
                    str(s.wrong), f"{100 * s.precision:.0f}%" if s.issued else "-"]
        rows.append(row)
    return table(rows, ["tier", "proved", "bad", "prec", "refuted", "bad", "prec"])


def failure_table(results: list[dict]) -> str:
    """Paper Table 6's 'no proof attempted' columns: attempts that failed at type checking / audit."""
    rows = []
    for arm, rs in by_arm(results).items():
        counts: dict[str, int] = defaultdict(int)
        for r in rs:
            for f in r.get("failures", []):
                counts[f] += 1
        never = sum(1 for r in rs if not r.get("reached_proof") and r["verdict"] == ABSTAIN)
        rows.append([arm, str(len(rs)), str(never)] +
                    [str(counts.get(k, 0)) for k in ("type", "audit", "witness", "proof", "reachability",
                                                     "llm-error")])
    return table(rows, ["arm", "entries", "never reached proof", "type", "audit", "witness", "proof", "reachability",
                        "llm-error"])


def wrong_verdicts(results: list[dict]) -> str:
    """Paper Table 5: every verdict contradicting its label."""
    rows = []
    for r in results:
        if r["verdict"] in (VERIFIED, REFUTED) and not is_right(r["verdict"], r["label"]):
            kind = "reference" if r["label"] == CORRECT else "mutant"
            rows.append([r["arm"], r["entry_id"], kind, r["verdict"], "; ".join(r.get("witness", []))[:80]])
    return table(rows, ["arm", "entry", "entry kind", "verdict", "witness"]) if rows else "(none)"

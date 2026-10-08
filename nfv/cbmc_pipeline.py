"""The bounded-verification NFV arm with CBMC (paper Sections 3.2, 4.3).

Refutation only. Stages mirror the Dafny arm with C as the formal language:
1a library code (C, from documentation), 2 tagged translation + harness,
1b ``nfv_pre``, 3 ``nfv_spec``. CBMC then searches for an execution of the
harness violating the ``nfv_spec`` assertion within a fixed unwinding, with its
built-in UB checks (bounds, pointers, overflow, conversions, division) on.

* a violating trace with no UB failure      -> ``refuted`` (the trace is the witness)
* UB failures                               -> goal-blind repair of the stage owning
                                               the failing function (attempt fails if
                                               the budget runs out)
* a clean run                               -> abstain (no counterexample in bounds)

There is no proof search: repair only makes the C well-formed and UB-free.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import dafny_artifact as A
from . import prompts as P
from .dafny_pipeline import ABSTAIN, REFUTED, ArmResult, Budget, Ctx, Log, StageFailure, _stage_loop
from .dataset import Entry
from .extract import extract_code
from .libdocs import library_docs
from .llm import Backend, LLMError
from .tools import Cbmc

SPEC_PROPERTY = "nfv_spec"
# Library and translation code may restrict executions to the domain where the C model is faithful to Python
# (e.g. int(float) only within the long long range). For bounded *refutation* an assumption only prunes
# executions, so every counterexample that survives is still a real one -- unlike a saturated or made-up value.
PRELUDE = "#define NFV_MODEL_DOMAIN(cond) __CPROVER_assume(cond)\n"
FORBIDDEN_C = re.compile(r"__CPROVER_|\bnondet_\w*\s*\(|\basm\b|#\s*include\s*[<\"](?!(stdbool|stddef|stdint|string|limits|math)\.h)")


def _c_functions(code: str) -> dict[str, tuple[int, int]]:
    """name -> (start token, end token) of C function definitions."""
    toks = A.tokenize(code)
    out = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.text == "(" and i > 0 and re.match(r"^[A-Za-z_]\w*$", toks[i - 1].text):
            try:
                close = A._match(toks, i, "(", ")")
            except A.ArtifactError:
                break
            if close + 1 < len(toks) and toks[close + 1].text == "{":
                # a definition only at brace depth 0
                depth = sum(1 if x.text == "{" else -1 if x.text == "}" else 0 for x in toks[:i])
                if depth == 0:
                    end = A._match(toks, close + 1)
                    out[toks[i - 1].text] = (i - 1, end)
                    i = end + 1
                    continue
        i += 1
    return out


def _function_text(code: str, name: str) -> str:
    toks = A.tokenize(code)
    span = _c_functions(code).get(name)
    return " ".join(t.text for t in toks[span[0]:span[1] + 1]) if span else ""


def check_harness(code: str, entry_point: str) -> list[str]:
    errs = []
    fns = _c_functions(code)
    if "nfv_harness" not in fns:
        return ["`void nfv_harness(void)` is missing"]
    h = _function_text(code, "nfv_harness")
    if len(re.findall(r"__CPROVER_assume \( nfv_pre \(", h)) != 1:
        errs.append("the harness must call `__CPROVER_assume(nfv_pre(...))` exactly once")
    if len(re.findall(r"__CPROVER_assert \( nfv_spec \(", h)) != 1 or f'"{SPEC_PROPERTY}"' not in h:
        errs.append('the harness must call `__CPROVER_assert(nfv_spec(..., return_value), "nfv_spec")` exactly once')
    if len(re.findall(rf"\b{re.escape(entry_point)} \(", h)) != 1:
        errs.append(f"the harness must call {entry_point} exactly once")
    if "nfv_pre" in fns or "nfv_spec" in fns:
        errs.append("nfv_pre/nfv_spec are written by later stages: declare them, do not define them")
    for name in fns:
        if name == "nfv_harness":
            continue
        if FORBIDDEN_C.search(_function_text(code, name).replace(" ", "")):
            errs.append(f"function {name} must be deterministic and plain C (no __CPROVER_*, nondet_*)")
    return errs


def _decl(code: str, name: str) -> str:
    m = re.search(rf"(?:_Bool|bool)\s+{name}\s*\([^;{{]*\)\s*;", code)
    return m.group(0) if m else f"bool {name}(...);"


def _plain(code: str, what: str) -> list[str]:
    errs = []
    if FORBIDDEN_C.search(code):
        errs.append(f"the {what} must be plain deterministic C (no __CPROVER_*, nondet_*, extra #includes)")
    return errs


def refutation_arm(entry: Entry, llm: Backend, cbmc: Cbmc, budget: Budget | None = None,
                   artifacts_dir: Path | None = None) -> ArmResult:
    budget = budget or Budget()
    res = ArmResult(entry_id=entry.id, arm="cbmc", label=entry.label)
    log = Log(res)
    ctx = Ctx(entry, llm, None, budget, log, artifacts_dir and artifacts_dir / "cbmc")  # type: ignore[arg-type]
    t0 = time.monotonic()

    def compiles(code: str) -> tuple[str, str] | None:
        err = cbmc.compile_check(PRELUDE + code)
        return ("type", err) if err else None

    def attempt() -> bool:
        theory = _stage_loop(ctx, "c.theory", P.c_theory(entry, library_docs(entry.source)),
                             lambda t: extract_code(t, ("c",)),
                             lambda c: ("audit", "\n".join(_plain(c, "library code"))) if _plain(c, "library code")
                             else compiles(c))
        ctx.save("1a_theory.c", theory)

        def check_translation(code):
            errs = A.check_provenance(code, entry.source) + check_harness(code, entry.entry_point)
            if errs:
                return "audit", "\n".join(errs)
            return compiles(theory + "\n" + code)

        translation = _stage_loop(ctx, "c.translate", P.c_translate(entry, theory),
                                  lambda t: extract_code(t, ("c",)), check_translation)
        ctx.save("2_translation.c", translation)
        prog_names = set(_c_functions(translation)) - {"nfv_harness"}

        def check_def(name, what):
            def check(code):
                errs = _plain(code, what)
                if name not in _c_functions(code):
                    errs.append(f"define `{name}`")
                bad = {t.text for t in A.tokenize(code)} & prog_names
                if bad:
                    errs.append(f"the {what} may not call the translated program ({', '.join(sorted(bad))})")
                if errs:
                    return "audit", "\n".join(errs)
                return compiles(theory + "\n" + translation + "\n" + code)
            return check

        pre = _stage_loop(ctx, "c.precondition", P.c_precondition(entry, _decl(translation, "nfv_pre")),
                          lambda t: extract_code(t, ("c",)), check_def("nfv_pre", "precondition"))
        spec = _stage_loop(ctx, "c.spec", P.c_spec(entry, _decl(translation, "nfv_spec"), theory),
                           lambda t: extract_code(t, ("c",)), check_def("nfv_spec", "postcondition"))
        parts = {"theory": theory, "translation": translation, "pre": pre, "spec": spec}
        owner = {fn: k for k, code in parts.items() for fn in _c_functions(code)}

        for rnd in range(budget.rounds + 1):
            full = PRELUDE + "\n".join(parts.values())
            ctx.save(f"cbmc_round{rnd}.c", full)
            r = cbmc.run(full)
            if r.timed_out:
                log(ctx.attempt, "cbmc", "timeout", rnd=rnd)
                res.failures.append("timeout")
                return False
            if r.compile_error:
                raise StageFailure("cbmc", "type", r.compile_error)
            ub = [p for p in r.failed() if p.description != SPEC_PROPERTY]
            violated = [p for p in r.failed() if p.description == SPEC_PROPERTY]
            if not ub:
                res.reached_proof = True
                if violated:
                    last = {}
                    for lhs, val in violated[0].trace_assignments:
                        last[lhs] = val
                    res.witness = [f"{k} = {v}" for k, v in last.items() if not k.startswith("return_value_")]
                    res.verdict, res.artifact = REFUTED, full
                    log(ctx.attempt, "cbmc", "counterexample", "\n".join(res.witness), rnd)
                else:
                    log(ctx.attempt, "cbmc", "no-counterexample", rnd=rnd)
                    res.failures.append("no-counterexample")
                return True  # a definitive answer for this attempt
            # goal-blind UB repair of the owning stage
            by_stage: dict[str, list[str]] = {}
            for p in ub:
                by_stage.setdefault(owner.get(p.function, "translation"), []).append(
                    f"{p.id}: {p.description}")
            log(ctx.attempt, "cbmc", "undefined-behaviour", repr(by_stage), rnd)
            if rnd == budget.rounds:
                break
            stage, msgs = next(iter(by_stage.items()))
            fb = "CBMC found undefined behaviour:\n" + "\n".join(msgs)
            prompt = {"theory": P.c_theory(entry, library_docs(entry.source)),
                      "translation": P.c_translate(entry, theory),
                      "pre": P.c_precondition(entry, _decl(translation, "nfv_pre")),
                      "spec": P.c_spec(entry, _decl(translation, "nfv_spec"), theory)}[stage]
            fixed = extract_code(ctx.ask(prompt + P.repair_suffix(parts[stage], fb, "c")), ("c",))
            if fixed is None:
                continue
            candidate = dict(parts, **{stage: fixed})
            if stage == "translation" and check_harness(fixed, entry.entry_point):
                continue
            if A.check_provenance(candidate["translation"], entry.source) or \
                    cbmc.compile_check(PRELUDE + "\n".join(candidate.values())):
                continue
            parts = candidate
        res.failures.append("ub")
        return False

    for k in range(1, budget.attempts + 1):
        ctx.attempt = k
        res.attempts_used = k
        try:
            if attempt():
                break
        except StageFailure as e:
            res.failures.append(e.kind)
            log(k, e.stage, "attempt-failed", f"{e.kind}: {e.detail}")
        except LLMError as e:
            res.failures.append("llm-error")
            log(k, "llm", "error", str(e))
    if res.verdict == ABSTAIN and res.failures:
        res.failure = res.failures[-1]
    res.seconds = round(time.monotonic() - t0, 2)
    return res

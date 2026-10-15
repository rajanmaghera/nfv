"""The proof-based NFV pipeline with Dafny (paper Section 3.1, 4.2).

Budget (paper Section 4.1): an *attempt* discards everything and restarts from
stage 1; a *repair round* re-prompts stage 4 only. Default 5 attempts x 10
rounds. Stages 1a/1b/2/3 have their own small repair loops whose only feedback
is resolution (syntax/type) errors and audit messages -- never a verifier goal.

Order within an attempt: 1a theory -> 2 translation -> 1b precondition -> 3 spec
-> 4 proof. (1b runs after 2 so the precondition can be written over the Dafny
parameter types; it still never sees the specification or the method body.)

Verification arm: goal S_F; verdict ``verified`` iff the artifact verifies and
passes every audit.
Refutation arm: reuses 1a/1b/2/3 byte-identically, negates S_F mechanically,
asks for a witness from the Python side, installs it as an extra precondition
and proves the negated goal. Verdict ``refuted`` iff it verifies, passes the
audits, and the reachability check (``ensures false`` must NOT verify) holds.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import dafny_artifact as A
from . import prompts as P
from . import trace
from .dataset import Entry
from .extract import clauses, extract_code, extract_labeled
from .libdocs import library_docs
from .llm import Backend, LLMError
from .tools import Dafny

NO_WITNESS: list[str] = []  # sentinel: the witness stage answered `// NONE`
NO_WITNESS_RE = re.compile(r"^\s*//\s*NONE\b", re.M)

VERIFIED = "verified"
REFUTED = "refuted"
ABSTAIN = "abstain"


@dataclass
class Budget:
    attempts: int = 5
    rounds: int = 10          # stage-4 proof repair rounds per attempt
    stage_rounds: int = 3     # repair rounds for each of stages 1a, 2, 1b, 3 and the witness


class StageFailure(Exception):
    def __init__(self, stage: str, kind: str, detail: str):
        super().__init__(f"{stage}: {kind}: {detail[:300]}")
        self.stage, self.kind, self.detail = stage, kind, detail


@dataclass
class FrontEnd:
    """Stages 1a, 2, 1b, 3 of one attempt (goal-blind up to stage 3)."""
    theory: str
    program: str               # stage-2 translation, no contract
    header: str
    pre: list[str]
    spec_helpers: str
    ensures: list[str]

    def artifact(self, requires: list[tuple[str, str]], ensures: list[tuple[str, str]], entry: str) -> str:
        prog = A.insert_contract(self.program, entry, requires, ensures)
        return A.assemble(self.theory, self.spec_helpers, prog)

    def verification_artifact(self, entry: str) -> str:
        return self.artifact([(A.PRE_TAG, c) for c in self.pre], [(A.POST_TAG, c) for c in self.ensures], entry)

    def negated_goal(self) -> str:
        return "!(" + " && ".join(f"({c})" for c in self.ensures) + ")"


@dataclass
class ArmResult:
    entry_id: str
    arm: str
    label: str
    verdict: str = ABSTAIN
    attempts_used: int = 0
    failure: str = ""            # type | audit | proof | witness | reachability | llm-error (last failure)
    failures: list[str] = field(default_factory=list)  # per attempt
    reached_proof: bool = False
    witness: list[str] = field(default_factory=list)
    artifact: str = ""
    events: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    llm_calls: int = 0

    def to_json(self) -> dict:
        return asdict(self)


class Log:
    def __init__(self, result: ArmResult):
        self.result = result

    def __call__(self, attempt: int, stage: str, status: str, detail: str = "", rnd: int | None = None):
        ev = {"attempt": attempt, "stage": stage, "status": status, "t": round(time.time(), 3)}
        if rnd is not None:
            ev["round"] = rnd
        if detail:
            ev["detail"] = detail[:2000]
        self.result.events.append(ev)
        trace.set_context(attempt=attempt)
        trace.emit("event", stage=stage, status=status, round=rnd, detail=detail[:4000] if detail else None)


class Ctx:
    """Everything a stage needs."""

    def __init__(self, entry: Entry, llm: Backend, dafny: Dafny, budget: Budget, log: Log,
                 artifacts_dir: Path | None = None):
        self.entry, self.llm, self.dafny, self.budget, self.log = entry, llm, dafny, budget, log
        self.artifacts_dir = artifacts_dir
        self.attempt = 0

    def ask(self, prompt: str) -> str:
        self.log.result.llm_calls += 1
        return self.llm.complete(prompt, system=P.SYSTEM).text

    def save(self, name: str, text: str):
        if self.artifacts_dir is not None:
            d = self.artifacts_dir / f"attempt_{self.attempt}"
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)


# ---------------------------------------------------------------- generic stage loop


def _stage_loop(ctx: Ctx, stage: str, prompt: str, parse, check) -> object:
    """Prompt, parse, check; on failure re-prompt with the feedback (stage_rounds times).

    ``parse(text) -> value | None``; ``check(value) -> (kind, feedback) | None``.
    """
    previous, feedback, kind = "", "", "type"
    for rnd in range(ctx.budget.stage_rounds + 1):
        p = prompt if rnd == 0 else prompt + P.repair_suffix(previous, feedback)
        text = ctx.ask(p)
        value = parse(text)
        if value is None:
            kind, feedback, previous = "type", "No usable fenced code block was found in your answer.", text[-3000:]
            ctx.log(ctx.attempt, stage, "parse-error", feedback, rnd)
            continue
        problem = check(value)
        if problem is None:
            ctx.log(ctx.attempt, stage, "ok", rnd=rnd)
            return value
        kind, feedback = problem
        previous = _render(value)
        ctx.log(ctx.attempt, stage, kind, feedback, rnd)
    raise StageFailure(stage, kind, feedback)


def _render(value) -> str:
    """A parsed stage answer, back in the form the stage was asked for."""
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):  # (spec helpers, ensures clauses)
        helpers, cl = value
        return (helpers + "\n" if helpers else "") + "\n".join(f"ensures {c}" for c in cl)
    return "\n".join(f"requires {c}" for c in value)


def _resolve_problem(ctx: Ctx, code: str) -> tuple[str, str] | None:
    r = ctx.dafny.resolve(code)
    if not r.ok or r.error_diags:
        return "type", r.feedback()
    ax = A.axiom_warnings(r.diags)
    if ax:
        return "audit", "\n".join(ax)
    return None


def _names_declared(program: str) -> set[str]:
    return {d.name for d in A.declarations(A.tokenize(program))}


def _references(code: str, names: set[str]) -> set[str]:
    return {t.text for t in A.tokenize(code)} & names


# ---------------------------------------------------------------- front end


def stage_theory(ctx: Ctx) -> str:
    def check(code):
        errs = A.check_forbidden(code, region="THEORY")
        if errs:
            return "audit", "\n".join(errs)
        if not code.strip() or not A.tokenize(code):
            return None
        return _resolve_problem(ctx, A.assemble(code, "", ""))

    code = _stage_loop(ctx, "1a.theory", P.theory(ctx.entry, library_docs(ctx.entry.source)),
                       lambda t: extract_code(t, ("dafny",)), check)
    ctx.save("1a_theory.dfy", code)
    return code


def stage_translate(ctx: Ctx, theory: str) -> str:
    entry = ctx.entry

    def check(code):
        errs = A.check_provenance(code, entry.source) + A.check_forbidden(code, region="PROGRAM")
        try:
            c = A.entry_contract(code, entry.entry_point)
            if c["requires"] or c["ensures"]:
                errs.append(f"method {entry.entry_point} must not have requires/ensures clauses at this stage")
            hdr = A.entry_header(code, entry.entry_point)
            if not re.search(r"returns\s*\(\s*return_value\s*:", hdr):
                errs.append(f"method {entry.entry_point} must return a single value named `return_value`")
            A.insert_contract(code, entry.entry_point, [], [])  # body brace on its own line
        except A.ArtifactError as e:
            errs.append(str(e))
        if errs:
            return "audit", "\n".join(errs)
        return _resolve_problem(ctx, A.assemble(theory, "", code))

    code = _stage_loop(ctx, "2.translate", P.translate(entry, theory), lambda t: extract_code(t, ("dafny",)), check)
    ctx.save("2_translation.dfy", code)
    return code


def _clause_problems(cl: list[str], forbidden_names: set[str], what: str) -> list[str]:
    errs = []
    for c in cl:
        errs += A.check_forbidden(c, region="SPEC")
        bad = _references(c, forbidden_names)
        if bad:
            errs.append(f"the {what} may not refer to the translated program ({', '.join(sorted(bad))})")
    return errs


def stage_precondition(ctx: Ctx, theory: str, program: str, header: str) -> list[str]:
    entry = ctx.entry
    prog_names = _names_declared(program) - {entry.entry_point}

    def check(cl):
        errs = _clause_problems(cl, prog_names | {entry.entry_point}, "precondition")
        if errs:
            return "audit", "\n".join(errs)
        art = A.assemble(theory, "", A.insert_contract(program, entry.entry_point,
                                                       [(A.PRE_TAG, c) for c in cl], []))
        prob = _resolve_problem(ctx, art)
        if prob:
            return prob
        if cl:  # the precondition must not be inconsistent
            params = A.entry_params(program, entry.entry_point)
            lemma = (f"lemma NFV_PreConsistency({params})\n" + "".join(f"  requires {c}\n" for c in cl)
                     + "  ensures false\n{}\n")
            r = ctx.dafny.verify(A.assemble(theory, "", lemma), filter_symbol="NFV_PreConsistency.")
            if r.ok and not r.error_diags:
                return "audit", "Dafny proved `false` from this precondition: it is unsatisfiable."
        return None

    def parse(text):
        code = extract_code(text, ("dafny",))
        return None if code is None else clauses(code, "requires")

    cl = _stage_loop(ctx, "1b.precondition", P.precondition(entry, theory, header), parse, check)
    ctx.save("1b_precondition.dfy", "\n".join(f"requires {c}" for c in cl) + "\n")
    return cl


def _parse_spec(text: str) -> tuple[str, list[str]] | None:
    blocks = extract_labeled(text, "dafny", ("helpers", "ensures"))
    if "ensures" in blocks:
        helpers, ens = blocks.get("helpers", ""), blocks["ensures"]
    else:
        code = extract_code(text, ("dafny",))
        if code is None:
            return None
        lines = code.splitlines()
        first = next((i for i, l in enumerate(lines) if l.strip().startswith("ensures")), None)
        if first is None:
            return None
        helpers, ens = "\n".join(lines[:first]), "\n".join(lines[first:])
    cl = clauses(ens, "ensures")
    return (helpers.strip("\n"), cl) if cl else None


def stage_spec(ctx: Ctx, theory: str, program: str, header: str, pre: list[str]) -> tuple[str, list[str]]:
    entry = ctx.entry
    prog_names = _names_declared(program)

    def check(value):
        helpers, cl = value
        errs = _clause_problems(cl, prog_names, "postcondition")
        errs += A.check_forbidden(helpers, region="SPEC")
        bad = _references(helpers, prog_names)
        if bad:
            errs.append(f"spec helpers may not refer to the translated program ({', '.join(sorted(bad))})")
        for d in A.declarations(A.tokenize(helpers)):
            if d.kind not in ("function", "predicate", "datatype", "const", "type"):
                errs.append(f"spec helpers may only define functions/predicates/types, not {d.kind} {d.name}")
        if errs:
            return "audit", "\n".join(errs)
        prog = A.insert_contract(program, entry.entry_point, [(A.PRE_TAG, c) for c in pre],
                                 [(A.POST_TAG, c) for c in cl])
        return _resolve_problem(ctx, A.assemble(theory, helpers, prog))

    helpers, cl = _stage_loop(ctx, "3.spec", P.spec(entry, theory, header), _parse_spec, check)
    ctx.save("3_spec.dfy", helpers + "\n" + "\n".join(f"ensures {c}" for c in cl) + "\n")
    return helpers, cl


def build_front_end(ctx: Ctx) -> FrontEnd:
    theory = stage_theory(ctx)
    program = stage_translate(ctx, theory)
    header = A.entry_header(program, ctx.entry.entry_point)
    pre = stage_precondition(ctx, theory, program, header)
    helpers, ens = stage_spec(ctx, theory, program, header, pre)
    return FrontEnd(theory=theory, program=program, header=header, pre=pre, spec_helpers=helpers, ensures=ens)


class FrontEndCache:
    """Shares stages 1a/2/1b/3 of attempt k between the refutation and verification arms."""

    def __init__(self):
        self._d: dict[tuple[str, int], FrontEnd | StageFailure] = {}
        self._lock = threading.Lock()

    def get(self, ctx: Ctx) -> FrontEnd:
        key = (ctx.entry.id, ctx.attempt)
        with self._lock:
            hit = self._d.get(key)
        if hit is None:
            try:
                hit = build_front_end(ctx)
            except StageFailure as e:
                hit = e
            with self._lock:
                self._d[key] = hit
        else:
            ctx.log(ctx.attempt, "front-end", "reused")
        if isinstance(hit, StageFailure):
            raise hit
        return hit


# ---------------------------------------------------------------- stage 4


def proof_search(ctx: Ctx, artifact: str, goal: str) -> str | None:
    """Repair rounds against the verifier. Returns the verified artifact or None."""
    entry = ctx.entry
    frozen = A.Frozen.of(artifact, entry.entry_point)
    art = artifact
    r = ctx.dafny.verify(art)
    ax = A.axiom_warnings(r.diags)
    if r.ok and not r.error_diags and not ax:
        ctx.log(ctx.attempt, "4.proof", "verified", rnd=0)
        return art
    feedback = r.feedback() + ("\n" + "\n".join(ax) if ax else "")
    ctx.log(ctx.attempt, "4.proof", "failed", feedback, 0)
    for rnd in range(1, ctx.budget.rounds + 1):
        text = ctx.ask(P.proof(entry, art, feedback, goal))
        new = extract_code(text, ("dafny",))
        if new is None:
            feedback = "No fenced dafny block with the complete file was found in your answer."
            ctx.log(ctx.attempt, "4.proof", "parse-error", feedback, rnd)
            continue
        errs = A.audit_proof_artifact(frozen, new, entry.source)
        if errs:
            feedback = "The artifact was REJECTED by the freeze/audit checks:\n- " + "\n- ".join(errs)
            ctx.log(ctx.attempt, "4.proof", "audit", feedback, rnd)
            art = new if _has_markers(new) else art
            continue
        art = new
        r = ctx.dafny.verify(art)
        ax = A.axiom_warnings(r.diags)
        if r.ok and not r.error_diags and not ax:
            ctx.log(ctx.attempt, "4.proof", "verified", rnd=rnd)
            return art
        feedback = r.feedback() + ("\n" + "\n".join(ax) if ax else "")
        ctx.log(ctx.attempt, "4.proof", "failed", feedback, rnd)
    return None


def _has_markers(text: str) -> bool:
    try:
        A.split_regions(text)
        return True
    except A.ArtifactError:
        return False


# ---------------------------------------------------------------- arms


def _run_arm(entry: Entry, arm: str, llm: Backend, dafny: Dafny, budget: Budget, body,
             artifacts_dir: Path | None) -> ArmResult:
    res = ArmResult(entry_id=entry.id, arm=arm, label=entry.label)
    log = Log(res)
    ctx = Ctx(entry, llm, dafny, budget, log, artifacts_dir)
    t0 = time.monotonic()
    trace.set_context(entry_id=entry.id, arm=arm)
    trace.emit("arm-start", label=entry.label)
    for attempt in range(1, budget.attempts + 1):
        ctx.attempt = attempt
        trace.set_context(attempt=attempt)
        res.attempts_used = attempt
        try:
            if body(ctx, res):
                res.failure = ""
                break
        except StageFailure as e:
            res.failures.append(e.kind)
            log(attempt, e.stage, "attempt-failed", f"{e.kind}: {e.detail}")
        except LLMError as e:
            res.failures.append("llm-error")
            log(attempt, "llm", "error", str(e))
        except A.ArtifactError as e:
            res.failures.append("audit")
            log(attempt, "artifact", "error", str(e))
    if res.verdict == ABSTAIN and res.failures:
        res.failure = res.failures[-1]
    res.seconds = round(time.monotonic() - t0, 2)
    trace.emit("arm-end", verdict=res.verdict, witness=res.witness, failures=res.failures, seconds=res.seconds,
               artifact=res.artifact)
    return res


def verification_arm(entry: Entry, llm: Backend, dafny: Dafny, budget: Budget | None = None,
                     artifacts_dir: Path | None = None, cache: FrontEndCache | None = None) -> ArmResult:
    budget = budget or Budget()
    cache = cache or FrontEndCache()

    def body(ctx: Ctx, res: ArmResult) -> bool:
        fe = cache.get(ctx)
        art = fe.verification_artifact(entry.entry_point)
        ctx.save("4_verify_initial.dfy", art)
        res.reached_proof = True
        proved = proof_search(ctx, art, "verification")
        if proved is None:
            res.failures.append("proof")
            return False
        ctx.save("4_verify_proved.dfy", proved)
        res.verdict, res.artifact = VERIFIED, proved
        return True

    return _run_arm(entry, "verify", llm, dafny, budget, body, artifacts_dir and artifacts_dir / "verify")


def refutation_arm(entry: Entry, llm: Backend, dafny: Dafny, budget: Budget | None = None,
                   artifacts_dir: Path | None = None, cache: FrontEndCache | None = None) -> ArmResult:
    budget = budget or Budget()
    cache = cache or FrontEndCache()

    def body(ctx: Ctx, res: ArmResult) -> bool:
        fe = cache.get(ctx)
        neg = fe.negated_goal()
        prog_names = _names_declared(fe.program)

        def check(cl):
            errs = _clause_problems(cl, prog_names, "witness")
            if errs:
                return "audit", "\n".join(errs)
            art = fe.artifact([(A.PRE_TAG, c) for c in fe.pre] + [(A.WITNESS_TAG, c) for c in cl],
                              [(A.NEGATED_POST_TAG, neg)], entry.entry_point)
            return _resolve_problem(ctx, art)

        def parse(text):
            code = extract_code(text, ("dafny",))
            if code is not None and NO_WITNESS_RE.search(code) and not clauses(code, "requires"):
                return NO_WITNESS
            cl = clauses(code, "requires") if code else []
            return cl or None

        try:
            wit = _stage_loop(ctx, "witness", P.witness(entry, fe.header, fe.pre), parse,
                              lambda v: None if v is NO_WITNESS else check(v))
        except StageFailure as e:
            raise StageFailure(e.stage, "witness", e.detail) from e
        if wit is NO_WITNESS:
            # the model judges the program correct: abstain from refutation for this entry
            # (a definitive answer; further attempts would only re-ask the same question)
            res.failures.append("no-witness")
            ctx.log(ctx.attempt, "witness", "none-proposed")
            return True
        art = fe.artifact([(A.PRE_TAG, c) for c in fe.pre] + [(A.WITNESS_TAG, c) for c in wit],
                          [(A.NEGATED_POST_TAG, neg)], entry.entry_point)
        ctx.save("4_refute_initial.dfy", art)
        res.reached_proof = True
        proved = proof_search(ctx, art, "refutation")
        if proved is None:
            res.failures.append("proof")
            return False
        # goal shape: exactly the negated S_F (also enforced by the freeze audit)
        if A._toks(A.entry_contract(A.split_regions(proved)["PROGRAM"], entry.entry_point)["ensures"]) != \
                A._toks([neg]):
            res.failures.append("audit")
            ctx.log(ctx.attempt, "goal-shape", "failed")
            return False
        # reachability: with the goal replaced by `false` verification must fail
        regions = A.split_regions(proved)
        unreachable = A.assemble(regions["THEORY"], regions["SPEC"],
                                 A.replace_entry_ensures(regions["PROGRAM"], entry.entry_point, "false"))
        r = ctx.dafny.verify(unreachable)
        if r.ok and not r.error_diags:
            res.failures.append("reachability")
            ctx.log(ctx.attempt, "reachability", "failed", "`ensures false` verified: the witness is vacuous")
            return False
        ctx.log(ctx.attempt, "reachability", "ok")
        ctx.save("4_refute_proved.dfy", proved)
        res.verdict, res.artifact, res.witness = REFUTED, proved, wit
        return True

    return _run_arm(entry, "refute", llm, dafny, budget, body, artifacts_dir and artifacts_dir / "refute")


def pipeline(entry: Entry, llm: Backend, dafny: Dafny, budget: Budget | None = None,
             artifacts_dir: Path | None = None) -> dict[str, ArmResult]:
    """Refute first; verify only what refutation leaves open (paper Table 3, 'pipeline')."""
    cache = FrontEndCache()
    out = {"refute": refutation_arm(entry, llm, dafny, budget, artifacts_dir, cache)}
    if out["refute"].verdict != REFUTED:
        out["verify"] = verification_arm(entry, llm, dafny, budget, artifacts_dir, cache)
    return out


def dump(result: ArmResult) -> str:
    return json.dumps(result.to_json())

"""Command line: `python -m nfv <command>` (or `nfv <command>` inside `nix develop`).

  check                       verifier versions, dataset summary
  show ENTRY_ID               print an entry (e.g. HumanEval_2/mutant)
  prompt STAGE ENTRY_ID       print a stage's first prompt (no LLM call)
  run --arm ARM ...           run an arm over (a subset of) the benchmark
  report RUN_DIR [...]        paper-style tables from one or more runs

Arms: verify | refute | pipeline (refute, then verify) | cbmc | judge | unstaged
Backends: claude (`claude -p`) | local (OpenAI-compatible server, e.g. llama.cpp
`llama-server`) | ollama | vllm | replay:<transcript.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import metrics as M
from . import prompts as P
from .dataset import DEFAULT_DATASET, Entry, load_entries, select
from .llm import USAGE, Transcript, UsageBudgetExceeded, make_backend

ARMS = ("verify", "refute", "pipeline", "cbmc", "judge", "unstaged")


def _entries(args) -> list[Entry]:
    entries = load_entries(args.dataset)
    tasks = [int(t) for t in args.tasks.split(",")] if getattr(args, "tasks", None) else None
    ids = args.ids.split(",") if getattr(args, "ids", None) else None
    return select(entries, ids=ids, tasks=tasks, kind=getattr(args, "kind", None), limit=getattr(args, "limit", None))


# ---------------------------------------------------------------- commands


def cmd_check(args) -> int:
    from .tools import tool_versions
    for k, v in tool_versions().items():
        print(f"{k:6s} {v}")
    entries = load_entries(args.dataset)
    tiers: dict[str, int] = {}
    for e in entries:
        tiers[e.tier] = tiers.get(e.tier, 0) + 1
    print(f"dataset {args.dataset}: {len(entries)} entries, "
          f"{sum(e.is_correct for e in entries)} correct / {sum(not e.is_correct for e in entries)} incorrect; "
          f"entries per tier {dict(sorted(tiers.items()))}")
    return 0


def cmd_show(args) -> int:
    e = select(load_entries(args.dataset), ids=[args.entry])
    if not e:
        print(f"no entry {args.entry}", file=sys.stderr)
        return 1
    print(json.dumps(e[0].to_json(), indent=2))
    return 0


def cmd_prompt(args) -> int:
    from .libdocs import library_docs
    e = select(load_entries(args.dataset), ids=[args.entry])[0]
    hdr = f"method {e.entry_point}(...) returns (return_value: ...)"
    builders = {
        "theory": lambda: P.theory(e, library_docs(e.source)),
        "translate": lambda: P.translate(e, "// (theory)"),
        "precondition": lambda: P.precondition(e, "// (theory)", hdr),
        "spec": lambda: P.spec(e, "// (theory)", hdr),
        "witness": lambda: P.witness(e, hdr, []),
        "judge": lambda: P.judge(e),
        "unstaged": lambda: P.unstaged(e),
        "c.theory": lambda: P.c_theory(e, library_docs(e.source)),
        "c.translate": lambda: P.c_translate(e, "/* library */"),
    }
    if args.stage not in builders:
        print(f"stages: {', '.join(builders)}", file=sys.stderr)
        return 1
    print(builders[args.stage]())
    return 0


def cmd_run(args) -> int:
    from .dafny_pipeline import Budget, pipeline, refutation_arm, verification_arm
    from .tools import Cbmc, Dafny

    entries = _entries(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    done = set()
    if results_path.exists() and not args.fresh:
        for r in M.load_results(results_path):
            done.add((r["entry_id"], r.get("run_arm", r["arm"])))
    todo = [e for e in entries if (e.id, args.arm) not in done]
    config = {k: v for k, v in vars(args).items() if k != "func"}
    config["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (out / "config.json").write_text(json.dumps(config, indent=2, default=str))
    print(f"{len(todo)} entries to run ({len(entries) - len(todo)} already done) -> {out}", file=sys.stderr)
    if not todo:
        return 0

    backend = make_backend(args.backend, model=args.model, base_url=args.base_url, claude_args=args.claude_args,
                           temperature=args.temperature, max_tokens=args.max_tokens, timeout=args.llm_timeout,
                           effort=args.effort, isolation=args.claude_isolation)
    transcript = Transcript(backend, out / "transcript.jsonl", {"run_arm": args.arm})
    budget = Budget(attempts=args.attempts, rounds=args.rounds, stage_rounds=args.stage_rounds)
    dafny = Dafny(time_limit=args.dafny_time_limit, cores=args.dafny_cores)
    if args.arm in ("verify", "refute", "pipeline", "unstaged") and not dafny.available:
        print("dafny not found (run inside `nix develop`, or set NFV_DAFNY)", file=sys.stderr)
        return 2
    cbmc = Cbmc(unwind=args.cbmc_unwind)
    if args.arm == "cbmc" and not cbmc.available:
        print("cbmc not found (run inside `nix develop`, or set NFV_CBMC)", file=sys.stderr)
        return 2
    lock = threading.Lock()
    if args.trace:
        from . import trace
        trace.enable(out / "trace.jsonl")

    def run_one(e: Entry) -> list[dict]:
        from . import baselines, cbmc_pipeline, trace
        trace.set_context(entry_id=e.id, arm=args.arm, attempt=None)
        llm = transcript.with_context(entry_id=e.id)
        adir = out / "artifacts" / e.id.replace("/", "__")
        if args.arm == "verify":
            rs = [verification_arm(e, llm, dafny, budget, adir)]
        elif args.arm == "refute":
            rs = [refutation_arm(e, llm, dafny, budget, adir)]
        elif args.arm == "pipeline":
            rs = list(pipeline(e, llm, dafny, budget, adir).values())
        elif args.arm == "cbmc":
            rs = [cbmc_pipeline.refutation_arm(e, llm, cbmc, budget, adir)]
        elif args.arm == "judge":
            rs = [baselines.llm_judge(e, llm)]
        elif args.arm == "unstaged":
            rs = [baselines.unstaged(e, llm, dafny, budget, adir / "unstaged")]
        else:
            raise ValueError(args.arm)
        recs = []
        for r in rs:
            rec = r.to_json()
            rec["run_arm"] = args.arm
            if not args.keep_events:
                rec["events"] = [ev for ev in rec["events"] if ev["status"] not in ("ok",)]
            recs.append(rec)
        with lock, open(results_path, "a") as fh:
            for rec in recs:
                fh.write(json.dumps(rec) + "\n")
        return recs

    if args.stop_at_usage is not None:
        USAGE.soft, USAGE.hard = args.stop_at_usage, args.stop_at_usage + args.usage_slack
        print(f"usage guard: no new entries at {USAGE.soft:.0%} session usage, abort in-flight at {USAGE.hard:.0%}",
              file=sys.stderr)
    n_done = 0
    queue = list(todo)
    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {}
        while queue and len(futs) < max(1, args.jobs):
            e = queue.pop(0)
            futs[pool.submit(run_one, e)] = e
        while futs:
            done = next(as_completed(futs))
            e = futs.pop(done)
            n_done += 1
            try:
                recs = done.result()
                summary = ", ".join(f"{r['arm']}={r['verdict']}" for r in recs)
            except UsageBudgetExceeded as ex:
                summary = f"ABORTED (not recorded): {ex}"
            except Exception as ex:  # noqa: BLE001  (keep the run going; the entry stays unscored)
                summary = f"CRASH {type(ex).__name__}: {ex}"
            usage = f" [usage: {USAGE.describe()}]" if USAGE.utilization else ""
            print(f"[{n_done}/{len(todo)}] {e.id} ({e.label}): {summary}{usage}", file=sys.stderr, flush=True)
            if USAGE.soft_reached() and not stopped:
                stopped = True
                print(f"usage guard: {USAGE.describe()} >= {USAGE.soft:.0%}; not starting the remaining "
                      f"{len(queue)} entries", file=sys.stderr, flush=True)
            if not stopped and queue:
                e2 = queue.pop(0)
                futs[pool.submit(run_one, e2)] = e2
    if results_path.exists() and results_path.stat().st_size:
        print(M.main_table(M.load_results(results_path)))
    else:
        print("(no results recorded)")
    return 0


def cmd_report(args) -> int:
    results: list[dict] = []
    for d in args.runs:
        results += M.load_results(d)
    entries = load_entries(args.dataset)
    print("== Main table (paper Table 3 layout)")
    print(M.main_table(results))
    if any(r["arm"] in ("verify", "refute") for r in results):
        print("\n== By library tier (paper Table 4 layout)")
        print(M.tier_table(results, entries))
    print("\n== Failure reasons per attempt (paper Table 6 'no proof attempted')")
    print(M.failure_table(results))
    print("\n== Verdicts contradicting the label (paper Table 5)")
    print(M.wrong_verdicts(results))
    return 0


# ---------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nfv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(func=cmd_check)
    s = sub.add_parser("show")
    s.add_argument("entry")
    s.set_defaults(func=cmd_show)
    s = sub.add_parser("prompt")
    s.add_argument("stage")
    s.add_argument("entry")
    s.set_defaults(func=cmd_prompt)

    r = sub.add_parser("run", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    r.add_argument("--arm", choices=ARMS, required=True)
    r.add_argument("--backend", default="claude", help="claude | local | ollama | vllm | replay:<file>")
    r.add_argument("--model", default=None, help="model alias/name passed to the backend")
    r.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint for local backends")
    r.add_argument("--claude-args", default=None,
                   help='override the extra `claude -p` flags (default: --output-format json --tools "" ...)')
    r.add_argument("--effort", default=None, help="claude backend: --effort level (low|medium|high|...)")
    r.add_argument("--claude-isolation", choices=("safe-mode", "bare", "none"), default=None,
                   help="claude backend isolation (default safe-mode; bare needs ANTHROPIC_API_KEY)")
    r.add_argument("--temperature", type=float, default=0.2)
    r.add_argument("--max-tokens", type=int, default=8192)
    r.add_argument("--llm-timeout", type=float, default=None)
    r.add_argument("--attempts", type=int, default=5)
    r.add_argument("--rounds", type=int, default=10)
    r.add_argument("--stage-rounds", type=int, default=3)
    r.add_argument("--dafny-time-limit", type=int, default=30)
    r.add_argument("--dafny-cores", default="2")
    r.add_argument("--cbmc-unwind", type=int, default=8)
    r.add_argument("--tasks", default=None, help="comma-separated HumanEval task numbers")
    r.add_argument("--ids", default=None, help="comma-separated entry ids")
    r.add_argument("--kind", choices=("reference", "mutant"), default=None)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--jobs", type=int, default=1)
    r.add_argument("--out", default="runs/latest")
    r.add_argument("--fresh", action="store_true", help="ignore results already in --out")
    r.add_argument("--keep-events", action="store_true", help="keep successful-stage events in results")
    r.add_argument("--trace", action="store_true",
                   help="write <out>/trace.jsonl: every LLM call, verifier run and event, in order (for demos)")
    r.add_argument("--stop-at-usage", type=float, default=None,
                   help="claude backend: stop starting entries once the 5-hour session utilization reaches this "
                        "fraction (e.g. 0.75); in-flight entries abort (unrecorded) past it + --usage-slack")
    r.add_argument("--usage-slack", type=float, default=0.03)
    r.set_defaults(func=cmd_run)

    rep = sub.add_parser("report")
    rep.add_argument("runs", nargs="+")
    rep.set_defaults(func=cmd_report)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)

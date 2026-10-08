#!/usr/bin/env python3
"""Per-entry LLM calls, cost, wall time and notable events of a run directory."""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

run = Path(sys.argv[1])
calls, cost, secs = Counter(), defaultdict(float), defaultdict(float)
stages = defaultdict(Counter)
for line in open(run / "transcript.jsonl"):
    r = json.loads(line)
    e = r.get("entry_id", "?")
    calls[e] += 1
    cost[e] += (r.get("meta") or {}).get("total_cost_usd") or 0
    secs[e] += r.get("seconds") or 0
    stages[e][r["stage"]] += 1
print(f"{'entry':26s} {'calls':>5s} {'cost$':>6s} {'llm s':>6s}  stages")
for e in sorted(calls):
    print(f"{e:26s} {calls[e]:5d} {cost[e]:6.2f} {secs[e]:6.0f}  {dict(stages[e])}")
print(f"{'TOTAL':26s} {sum(calls.values()):5d} {sum(cost.values()):6.2f} {sum(secs.values()):6.0f}")
print("\nnon-ok events (stage, status):")
for line in open(run / "results.jsonl"):
    r = json.loads(line)
    ev = Counter((e["stage"], e["status"]) for e in r["events"] if e["status"] not in ("ok", "reused"))
    print(f"  {r['entry_id']:26s} {r['arm']:7s} {r['verdict']:9s} attempts={r['attempts_used']} {dict(ev)}")

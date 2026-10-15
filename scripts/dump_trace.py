#!/usr/bin/env python3
"""Readable Markdown dump of a traced run (`nfv run --trace`): every LLM call (full prompt and response),
every verifier run (command, input file, output) and every pipeline event, in order."""
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
out = Path(sys.argv[2]) if len(sys.argv) > 2 else run / "trace.md"
recs = [json.loads(l) for l in open(run / "trace.jsonl")]
t0 = recs[0]["t"]
md = [f"# Trace of `{run}`\n", f"{len(recs)} records: "
      f"{sum(r['kind'] == 'llm' for r in recs)} LLM calls, {sum(r['kind'] == 'verifier' for r in recs)} verifier "
      f"runs, {sum(r['kind'] == 'event' for r in recs)} events.\n"]
fence = lambda s, lang="": f"\n````{lang}\n{s.rstrip()}\n````\n"  # noqa: E731
for r in recs:
    head = f"t+{r['t'] - t0:6.1f}s · {r.get('entry_id', '')} · {r.get('arm', '')} · attempt {r.get('attempt', '-')}"
    k = r["kind"]
    if k == "arm-start":
        md.append(f"\n---\n\n## [{r['seq']}] ▶ {r['entry_id']} — {r['arm']} arm (label: {r['label']})\n\n{head}\n")
    elif k == "arm-end":
        md.append(f"\n### [{r['seq']}] ■ verdict: **{r['verdict']}** ({r['seconds']} s)\n\n{head}\n")
        if r.get("witness"):
            md.append(f"witness: `{'; '.join(r['witness'])}`\n")
        if r.get("artifact"):
            md.append("final artifact:" + fence(r["artifact"], "dafny"))
    elif k == "llm":
        md.append(f"\n### [{r['seq']}] LLM call · stage `{r['stage']}` · {r['seconds']} s · ${r.get('cost_usd') or 0:.3f}\n\n{head}\n")
        md.append("<details><summary>system prompt</summary>\n" + fence(r.get("system") or "") + "</details>\n")
        md.append("**prompt**" + fence(r["prompt"]) + "**response**" + fence(r["response"]))
    elif k == "verifier":
        md.append(f"\n### [{r['seq']}] verifier · `{r['command']}` · exit {r['exit_code']} · "
                  f"{'OK' if r['ok'] else 'FAILED'} · {r['seconds']} s\n\n{head}\n")
        md.append("<details><summary>input file</summary>\n" + fence(r["input"], "dafny") + "</details>\n")
        md.append("output" + fence(r["output"]))
    elif k == "event":
        rnd = f" round {r['round']}" if r.get("round") is not None else ""
        md.append(f"\n- [{r['seq']}] event `{r['stage']}`{rnd}: **{r['status']}**"
                  + (fence(r["detail"]) if r.get("detail") else "\n"))
out.write_text("\n".join(md))
print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")

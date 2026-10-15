#!/usr/bin/env python3
"""Build an animated slide deck (HTML) from a traced run (`nfv run --trace`).

    python3 scripts/make_demo.py demo/HumanEval_42/run demo/HumanEval_42/deck.html

Every LLM call becomes a slide in three phases (what the stage sees -> the model's answer -> the checks that ran
on it); verifier-only steps (initial proof attempt, reachability) and verdicts get their own slides.
`deck.html#s=N&p=K` shows slide N at phase K (used for GIF frames); `deck.html?print` shows every slide fully,
one per page (used for the PDF). `scripts/render_demo.sh` turns it into frames, a GIF and a PDF.
"""

from __future__ import annotations

import html
import time
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfv import dafny_artifact as A  # noqa: E402
from nfv.dataset import load_entries, select  # noqa: E402
from nfv.extract import extract_code  # noqa: E402

STAGES = [("dafny.theory", "1a", "Library theory"), ("dafny.translate", "2", "Translate"),
          ("dafny.precondition", "1b", "Precondition"), ("dafny.spec", "3", "Specification"),
          ("dafny.witness", "W", "Witness"), ("proof", "4", "Proof search"), ("reach", "R", "Reachability"),
          ("verdict", "✓", "Verdict")]
STAGE_INFO = {
    "dafny.theory": ("Formalize the Python library surface the program calls (E_F), from the docs.",
                     ["Python source", "library docstrings"], ["postcondition S_L"]),
    "dafny.translate": ("Translate the program line by line into Dafny; every line tagged src:/synth:.",
                        ["Python source (numbered)", "frozen theory"], ["postcondition S_L"]),
    "dafny.precondition": ("State the input assumptions the docstring makes, as `requires`.",
                           ["docstring", "signatures", "theory"], ["S_L", "translation body"]),
    "dafny.spec": ("Translate the Python postcondition S_L into Dafny `ensures` (S_F), faithfully.",
                   ["S_L", "docstring", "signatures", "theory"], ["translation body"]),
    "dafny.witness": ("Propose an input that violates S_L (or answer NONE if there is none).",
                      ["Python source", "S_L", "precondition"], ["Dafny translation body"]),
    "dafny.proof": ("Proof search: add ghost proof code only (invariants, asserts, lemmas). Everything else is frozen.",
                    ["assembled artifact", "verifier errors"], ["- (may only add ghost code)"]),
    "dafny.refute.proof": ("Prove the NEGATED postcondition on the witness input. Code and goal are frozen.",
                           ["assembled artifact", "verifier errors"], ["- (may only add ghost code)"]),
}

DAFNY_KW = r"\b(method|function|predicate|lemma|returns|requires|ensures|invariant|decreases|var|while|if|then|else|" \
           r"return|forall|exists|assert|ghost|seq|int|real|bool|string|nat|true|false|old|calc|by)\b"
PY_KW = r"\b(def|return|for|in|range|len|if|else|elif|assert|all|import|from|not|and|or|lambda|None|True|False)\b"


def hl(code: str, lang: str) -> str:
    """Tiny syntax highlighter (escapes HTML)."""
    out = []
    kw = re.compile(DAFNY_KW if lang == "dafny" else PY_KW)
    for line in code.split("\n"):
        cm = re.search(r"(//|#)(?![^\"']*[\"'][^\"']*$)", line) if lang in ("dafny", "python") else None
        cpos = None
        if lang == "dafny":
            m = re.search(r"//", line)
            cpos = m.start() if m else None
        elif lang == "python":
            m = re.search(r"#", line)
            cpos = m.start() if m else None
        del cm
        code_part, comment = (line[:cpos], line[cpos:]) if cpos is not None else (line, "")
        esc = html.escape(code_part)
        esc = re.sub(r"(&quot;.*?&quot;|&#x27;.*?&#x27;|\"[^\"]*\"|'[^']*')", r'<span class="s">\1</span>', esc)
        esc = kw.sub(r'<span class="k">\1</span>', esc)
        esc = re.sub(r"(?<![\w.#])(\d+(?:\.\d+)?)(?![\w;])", r'<span class="n">\1</span>', esc)
        if comment:
            cls = "c tag" if re.match(r"//\s*(src|synth):", comment) else "c"
            esc += f'<span class="{cls}">{html.escape(comment)}</span>'
        out.append(esc)
    return "\n".join(out)


def code_block(code: str, lang: str, max_lines: int = 28, added: set[int] | None = None, cls: str = "") -> str:
    lines = code.rstrip("\n").split("\n")
    more = len(lines) - max_lines
    if more > 0:
        lines = lines[:max_lines]
    body = hl("\n".join(lines), lang).split("\n")
    if added:
        body = [f'<span class="add">{l}</span>' if i in added else l for i, l in enumerate(body)]
    tail = f'\n<span class="c">… {more} more lines</span>' if more > 0 else ""
    return f'<pre class="code {cls}">' + "\n".join(body) + tail + "</pre>"


def prose_of(resp: str, limit: int = 330) -> str:
    p = re.sub(r"```.*?```", "", resp, flags=re.S).strip()
    p = re.sub(r"\s+", " ", p)
    if len(p) > limit:
        p = p[:limit].rsplit(" ", 1)[0] + " …"
    p = html.escape(p)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", p)


def first_block(prompt: str, lang: str = "python") -> str:
    m = re.search(rf"```{lang}\n(.*?)```", prompt, re.S)
    return m.group(1) if m else ""


def section(prompt: str, title: str) -> str:
    m = re.search(rf"## {re.escape(title)}\n```\w*\n(.*?)```", prompt, re.S)
    return m.group(1) if m else ""


def program_region(art: str) -> str:
    try:
        return A.split_regions(art)["PROGRAM"].strip("\n")
    except A.ArtifactError:
        return art


def proof_view(program: str, added: set[int], max_lines: int = 21) -> tuple[str, set[int]]:
    """Only the lines proof search added, with one line of context, `⋮` between hunks."""
    lines = program.splitlines()
    keep = set(range(min(2, len(lines))))
    for i in added:
        keep |= {i - 1, i, i + 1}
    keep = sorted(k for k in keep if 0 <= k < len(lines))
    out, marks, prev = [], set(), None
    for k in keep:
        if prev is not None and k != prev + 1:
            out.append("  ⋮")
        if k in added:
            marks.add(len(out))
        out.append(lines[k])
        prev = k
    if len(out) > max_lines:
        out = out[:max_lines - 1] + [f"  ⋮ ({len(added)} lines added in total)"]
        marks = {m for m in marks if m < max_lines - 1}
    return "\n".join(out), marks


def added_lines(new: str, old: str) -> set[int]:
    old_set = {l.strip() for l in old.splitlines()}
    return {i for i, l in enumerate(new.splitlines()) if l.strip() and l.strip() not in old_set}


# ---------------------------------------------------------------- build steps


def build(run: Path):
    recs = [json.loads(l) for l in open(run / "trace.jsonl")]
    entries = {e.id: e for e in load_entries()}
    t0 = recs[0]["t"]
    slides: list[dict] = []
    totals = {"llm": 0, "cost": 0.0, "dafny": 0}
    i = 0
    last_artifact = ""
    while i < len(recs):
        r = recs[i]
        k = r["kind"]
        common = {"entry": r.get("entry_id"), "arm": r.get("arm"), "t": r["t"] - t0}
        if k == "arm-start":
            slides.append({**common, "type": "arm", "label": r.get("label")})
            i += 1
            continue
        if k == "arm-end":
            slides.append({**common, "type": "verdict", "verdict": r["verdict"], "witness": r.get("witness"),
                           "artifact": r.get("artifact", ""), "failures": r.get("failures"), "seconds": r["seconds"]})
            i += 1
            continue
        if k == "llm":
            totals["llm"] += 1
            totals["cost"] += r.get("cost_usd") or 0
            checks, j = [], i + 1
            while j < len(recs) and recs[j]["kind"] in ("verifier", "event") and not (
                    recs[j]["kind"] == "verifier" and r["stage"] in ("dafny.proof", "dafny.refute.proof")
                    and any(c["kind"] == "verifier" for c in checks)):
                if recs[j]["kind"] == "verifier" and r["stage"] in ("dafny.witness",) and \
                        any(c["kind"] == "verifier" for c in checks):
                    break
                # an initial proof attempt after the witness is its own slide
                if recs[j]["kind"] == "verifier" and "verify" in recs[j]["command"] and \
                        r["stage"] not in ("dafny.proof", "dafny.refute.proof"):
                    break
                checks.append(recs[j])
                if recs[j]["kind"] == "verifier":
                    totals["dafny"] += 1
                    last_artifact = recs[j]["input"]
                j += 1
            resp_code = extract_code(r["response"]) or ""
            slide = {**common, "type": "llm", "stage": r["stage"], "seconds": r["seconds"], "cost": r.get("cost_usd"),
                     "prompt": r["prompt"], "response": r["response"], "code": resp_code, "checks": checks,
                     "totals": dict(totals)}
            if r["stage"] in ("dafny.proof", "dafny.refute.proof"):
                prev = program_region(slides[-1].get("artifact_in", "")) if slides and slides[-1]["type"] == "verifier" else ""
                slide["program"] = program_region(resp_code)
                slide["added"] = added_lines(slide["program"], prev)
            slides.append(slide)
            i = j
            continue
        if k == "verifier":
            totals["dafny"] += 1
            evs, j = [], i + 1
            while j < len(recs) and recs[j]["kind"] == "event":
                evs.append(recs[j])
                j += 1
            kind = "reach" if any(e["stage"] == "reachability" for e in evs) else "proof0"
            slides.append({**common, "type": "verifier", "kind": kind, "command": r["command"], "artifact_in": r["input"],
                           "output": r["output"], "ok": r["ok"], "seconds": r["seconds"], "events": evs,
                           "totals": dict(totals)})
            last_artifact = r["input"]
            i = j
            continue
        if k == "event":
            if r["stage"] == "front-end":
                slides.append({**common, "type": "note", "text": "Stages 1a, 2, 1b and 3 are reused byte-identically "
                               "from the refutation arm (same attempt): only the goal changes."})
            i += 1
            continue
        i += 1
    return slides, entries, totals, recs


# ---------------------------------------------------------------- render


def stage_key(s: dict) -> str:
    if s["type"] == "llm":
        return "proof" if s["stage"] in ("dafny.proof", "dafny.refute.proof") else s["stage"]
    if s["type"] == "verifier":
        return "reach" if s["kind"] == "reach" else "proof"
    if s["type"] == "verdict":
        return "verdict"
    return ""


def tracker(s: dict) -> str:
    cur = stage_key(s)
    names = [x[0] for x in STAGES]
    idx = names.index(cur) if cur in names else -1
    arm = s.get("arm")
    pills = []
    for n, (key, num, label) in enumerate(STAGES):
        if arm == "verify" and key in ("dafny.witness", "reach"):
            cls = "skip"
        elif n < idx:
            cls = "done"
        elif n == idx:
            cls = "cur"
        else:
            cls = ""
        pills.append(f'<div class="pill {cls}"><b>{num}</b>{label}</div>')
    return '<div class="tracker">' + "".join(pills) + "</div>"


def act_title(s: dict) -> str:
    entry = s.get("entry") or ""
    which = "Correct program" if entry.endswith("reference") else "Buggy program"
    arm = {"refute": "refutation arm: look for a bug first", "verify": "verification arm: prove it correct"}.get(
        s.get("arm"), "")
    return f'<span class="act {"good" if "reference" in entry else "bad"}">{which}</span> <span class="arm">{arm}</span>'


def header(s: dict, n: int, total: int) -> str:
    t = s.get("t", 0)
    return (f'<header><div class="brand">NFV · HumanEval/42 <code>incr_list</code></div>'
            f'<div class="mid">{act_title(s) if s.get("entry") else ""}</div>'
            f'<div class="meta">t+{t:5.1f}s · {n}/{total}</div></header>')


def checks_html(checks: list[dict]) -> str:
    items = []
    for c in checks:
        if c["kind"] == "verifier":
            ok = c["ok"]
            cmd = c["command"].split(" --")[0]
            items.append(f'<div class="chk {"ok" if ok else "bad"}">{"✓" if ok else "✗"} <code>{html.escape(cmd)}</code>'
                         f' {"well-formed" if ok and "resolve" in cmd else ("verified" if ok else f"{c.get("errors", 0)} error(s)")}'
                         f' · {c["seconds"]:.2f}s</div>')
        else:
            st, status = c["stage"], c["status"]
            label = {"ok": "audits passed (provenance tags, frozen parts, no assumptions)",
                     "none-proposed": "model answered NONE → refutation abstains (no proof budget spent)",
                     "verified": "proof checked", "failed": "verifier rejected the proof"}.get(status, status)
            bad = status in ("failed", "audit", "parse-error")
            items.append(f'<div class="chk {"bad" if bad else "ok"}">{"✗" if bad else "✓"} <b>{html.escape(st)}</b>: {html.escape(label)}</div>')
    return '<div class="checks" data-phase="2">' + "".join(items) + "</div>" if items else ""


def llm_slide(s: dict, entries) -> str:
    stage = s["stage"]
    desc, sees, never = STAGE_INFO.get(stage, ("", [], []))
    p = s["prompt"]
    if stage in ("dafny.theory", "dafny.translate", "dafny.witness"):
        inp, lang, cap = first_block(p, "python").replace("# source.py\n", ""), "python", "Python program (source.py)"
        if stage == "dafny.witness":
            inp += "\n# postcondition S_L\n" + section_py(p)
    elif stage == "dafny.precondition":
        inp, lang, cap = first_block(p, "python"), "python", "signature + docstring"
    elif stage == "dafny.spec":
        inp, lang, cap = first_block(p, "python"), "python", "Python postcondition S_L"
        hdr = re.search(r"## Dafny method signature \(the translation\)\n```dafny\n(.*?)```", p, re.S)
        if hdr:
            inp += "\n# Dafny signature:\n# " + hdr.group(1).strip()
    else:
        inp = section(p, "Verifier / checker output") or ""
        lang, cap = "text", "verifier output fed back"
    sees_html = "".join(f'<span class="chip y">{html.escape(x)}</span>' for x in sees)
    never_html = "".join(f'<span class="chip n">{html.escape(x)}</span>' for x in never)
    if stage in ("dafny.proof", "dafny.refute.proof"):
        view, marks = proof_view(s["program"], s.get("added") or set())
        right_code = code_block(view, "dafny", 22, marks, "small")
        legend = ('<div class="legend"><span class="addk">green</span> = lines added by proof search '
                  f'({len(s.get("added") or [])} lines, all ghost code); ⋮ = unchanged frozen code</div>')
    else:
        right_code = code_block(s["code"] or "(empty)", "dafny", 26)
        legend = ""
    cmd = "claude -p --model opus --effort medium --safe-mode --tools \"\" (prompt on stdin)"
    return f"""
<div class="stagehead"><span class="badge llm">LLM call</span><h2>{html.escape(stage)}</h2><span class="desc">{html.escape(desc)}</span></div>
<div class="cols">
  <div class="col left">
    <div class="ptitle">What this stage sees</div>
    <div class="chips">{sees_html}{'<span class="sep">never sees:</span>' + never_html if never else ''}</div>
    <div class="cap">{html.escape(cap)}</div>
    {code_block(inp.strip(chr(10)), lang, 22)}
  </div>
  <div class="col right">
    <div class="ptitle">Opus answers <span class="time">{s['seconds']:.1f}s · ${s.get('cost') or 0:.3f}</span></div>
    <div class="waiting" data-only-phase="0"><div class="spinner"></div><code>{html.escape(cmd)}</code></div>
    <div data-phase="1">
      <blockquote>{prose_of(s['response'])}</blockquote>
      {right_code}{legend}
    </div>
  </div>
</div>
{checks_html(s['checks'])}"""


def section_py(p: str) -> str:
    m = re.search(r"## Python postcondition S_L\n```python\n(.*?)```", p, re.S)
    return m.group(1).strip() if m else ""


def verifier_slide(s: dict) -> str:
    out = s["output"].strip()
    out = re.sub(r"\n{2,}", "\n", out)
    if s["kind"] == "reach":
        title, desc = "Reachability check", ("The proved artifact again, with the goal replaced by "
                                              "<code>ensures false</code>. It must NOT verify, otherwise the witness "
                                              "would be vacuous (contradicting the precondition).")
        good = not s["ok"]
        verdict = "✓ fails as required: the witness is a real, reachable input" if good else "✗ vacuous witness"
    else:
        title, desc = "Proof attempt 0", ("The harness assembles THEORY + SPEC + PROGRAM with the frozen contract and "
                                          "runs the verifier before asking for any proof.")
        good = s["ok"]
        verdict = "✓ verified" if good else "✗ not proved yet: errors go back to the model (stage 4)"
    art = program_region(s["artifact_in"])
    if s["kind"] == "reach":
        art = "\n".join(l for l in art.splitlines() if not l.strip().startswith("// synth: proof"))
    return f"""
<div class="stagehead"><span class="badge dafny">Dafny</span><h2>{title}</h2><span class="desc">{desc}</span></div>
<div class="cols">
  <div class="col left">
    <div class="ptitle">Input: artifact.dfy (PROGRAM region)</div>
    {code_block(art, "dafny", 25, None, "small")}
  </div>
  <div class="col right">
    <div class="ptitle"><code>{html.escape(s['command'])}</code> <span class="time">{s['seconds']:.2f}s</span></div>
    <div data-phase="1">
      <pre class="code out {'okout' if s['ok'] else 'badout'}">{html.escape(out[-1800:])}</pre>
      <div class="bigcheck {'ok' if good else 'bad'}">{html.escape(verdict)}</div>
    </div>
  </div>
</div>"""


def verdict_slide(s: dict, entries) -> str:
    v = s["verdict"]
    entry = entries.get(s["entry"])
    label = entry.label if entry else ""
    right = (v == "verified" and label == "correct") or (v == "refuted" and label == "incorrect")
    big = {"verified": "VERIFIED", "refuted": "REFUTED", "abstain": "ABSTAIN"}[v]
    sub = {"verified": "machine-checked proof that the program satisfies S_F under E_F",
           "refuted": "machine-checked proof that the program violates S_F on the witness",
           "abstain": "no verdict from this arm"}[v]
    if v == "abstain" and "no-witness" in (s.get("failures") or []):
        sub = "the model found no counterexample → the pipeline moves on to verification"
    body = ""
    if s.get("artifact"):
        body = code_block(program_region(s["artifact"]), "dafny", 27, None, "small")
    wit = f'<div class="wit">witness: <code>{html.escape("; ".join(s["witness"]))}</code></div>' if s.get("witness") else ""
    match = ""
    if v != "abstain":
        match = f'<div class="label {"ok" if right else "bad"}">ground-truth label: {label} → {"correct verdict" if right else "WRONG verdict"}</div>'
    return f"""
<div class="verdictwrap">
  <div class="vleft">
    <div class="vbig {v}">{big}</div>
    <div class="vsub">{html.escape(sub)}</div>
    {wit}{match}
    <div class="vtime">{html.escape(s['arm'])} arm · {s['seconds']:.1f}s wall time</div>
  </div>
  <div class="vright">{body}</div>
</div>"""


def arm_slide(s: dict, entries) -> str:
    e = entries.get(s["entry"])
    if s["arm"] == "verify":
        what = "Refutation found nothing, so the pipeline now tries to <b>prove</b> the program correct."
    else:
        what = ("Pipeline order (paper, Table 3): first try to <b>refute</b> (find and prove a counterexample), "
                "then try to <b>verify</b> what is left.")
    src = e.source.split('"""')[-1].strip("\n") if e else ""
    sig = e.signature() if e else ""
    return f"""
<div class="armwrap">
  <div class="armtitle">{act_title(s)}</div>
  <p class="armwhat">{what}</p>
  {code_block(sig + chr(10) + src, "python", 12) if e and s['arm'] == 'refute' else ''}
  <div class="post">S_L: <code>{html.escape(e.postcondition) if e else ''}</code></div>
</div>"""


def title_slides(entries, recs, totals) -> list[str]:
    ref, mut = entries["HumanEval_42/reference"], entries["HumanEval_42/mutant"]
    body_ref = ref.source.split('"""')[-1].strip("\n")
    body_mut = mut.source.split('"""')[-1].strip("\n")
    t1 = f"""
<div class="title">
  <div class="kicker">Neuro-formal verification (arXiv:2608.21516) · replication demo</div>
  <h1>One benchmark entry, every call</h1>
  <p class="lead">HumanEval/42 <code>incr_list</code>, its correct version and a buggy mutant, through the staged
  Python → Dafny pipeline with <b>Claude Opus</b> (<code>claude -p</code>, isolated) and the <b>Dafny 4.11</b>
  verifier. Recorded live: {totals['llm']} LLM calls, {totals['dafny']} verifier runs.</p>
  <div class="cols2">
    <div><div class="cap">correct (reference)</div>{code_block(ref.signature() + chr(10) + body_ref, "python", 10)}</div>
    <div><div class="cap">buggy (mutant)</div>{code_block(mut.signature() + chr(10) + body_mut, "python", 10)}</div>
  </div>
  <div class="post">postcondition S_L (both): <code>{html.escape(ref.postcondition)}</code></div>
  <div class="settings"><span>model <b>claude-opus</b> · effort medium</span><span>isolation <b>--safe-mode</b>, no tools, empty cwd</span>
  <span>budget <b>2 attempts × 4 rounds</b></span><span>verifier <b>Dafny 4.11</b> (Z3)</span><span>recorded {time.strftime("%Y-%m-%d", time.localtime(recs[0]["t"]))}</span></div>
</div>"""
    t2 = """
<div class="title">
  <h1 class="h1s">The staged, goal-blind pipeline</h1>
  <div class="flow">
    <div class="fbox"><b>1a</b> Library theory<br><small>from docstrings</small></div><div class="arrow">→</div>
    <div class="fbox"><b>2</b> Translate<br><small>tagged, line by line</small></div><div class="arrow">→</div>
    <div class="fbox"><b>1b</b> Precondition<br><small>from the docstring</small></div><div class="arrow">→</div>
    <div class="fbox spec"><b>3</b> Specification<br><small>S_L → S_F</small></div><div class="arrow">→</div>
    <div class="fbox proof"><b>4</b> Proof search<br><small>ghost code only</small></div>
  </div>
  <div class="blind">← stages before 3 never see the property (goal-blind) →</div>
  <ul class="rules">
    <li>Every stage is one <code>claude -p</code> call; each answer is checked by <code>dafny resolve</code>
        (syntax/types) and mechanical audits before the next stage runs.</li>
    <li>Refutation reuses the same stages, negates the goal, asks for a <b>witness</b> input, and must
        <b>prove</b> the violation, then pass a <b>reachability</b> check.</li>
    <li>Frozen during proof search: library theory, spec, every <code>// src:</code> line and its code,
        the contract. Only ghost code may be added, which is checked by ghost-erasure.</li>
  </ul>
</div>"""
    return [t1, t2]


def summary_slide(slides, totals, recs) -> str:
    ends = [r for r in recs if r["kind"] == "arm-end"]
    rows = "".join(f"<tr><td>{html.escape(r['entry_id'])}</td><td>{r['arm']}</td>"
                   f"<td class='v {r['verdict']}'>{r['verdict']}</td><td><code>{html.escape('; '.join(r.get('witness') or []))}</code></td>"
                   f"<td>{r['seconds']:.1f}s</td></tr>" for r in ends)
    llm = [r for r in recs if r["kind"] == "llm"]
    ver = [r for r in recs if r["kind"] == "verifier"]
    return f"""
<div class="title">
  <h1 class="h1s">Result: both verdicts correct, both machine-checked</h1>
  <table class="sum"><tr><th>entry</th><th>arm</th><th>verdict</th><th>witness</th><th>time</th></tr>{rows}</table>
  <div class="stats">
    <div><b>{len(llm)}</b><span>LLM calls</span></div>
    <div><b>{sum(r['seconds'] for r in llm):.0f}s</b><span>model time</span></div>
    <div><b>{len(ver)}</b><span>Dafny runs</span></div>
    <div><b>{sum(r['seconds'] for r in ver):.1f}s</b><span>verifier time</span></div>
    <div><b>${sum(r.get('cost_usd') or 0 for r in llm):.2f}</b><span>list-price cost</span></div>
  </div>
  <p class="lead small">Full trace with every prompt, response and verifier output: <code>demo/HumanEval_42/trace.md</code>
  · raw: <code>demo/HumanEval_42/run/trace.jsonl</code> · reproduce: <code>nfv run --arm pipeline --tasks 42 --trace</code></p>
</div>"""


CSS = r"""
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--llm:#58a6ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#bc8cff}
*{box-sizing:border-box}code,pre,.wit code{font-variant-ligatures:none;font-feature-settings:'liga' 0,'calt' 0}html,body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}
.slide{width:1280px;height:720px;position:relative;overflow:hidden;background:var(--bg);display:none;padding:0 28px}
.slide.on{display:block}
header{display:flex;align-items:center;justify-content:space-between;height:46px;border-bottom:1px solid var(--border);font-size:14px}
.brand{color:var(--muted)} .brand code{color:var(--text)} .meta{color:var(--muted);font-variant-numeric:tabular-nums}
.act{font-weight:700;padding:2px 10px;border-radius:12px;font-size:13px}.act.good{background:#12361f;color:var(--ok)}
.act.bad{background:#3d1416;color:var(--bad)} .arm{color:var(--muted);font-size:13px;margin-left:6px}
.tracker{display:flex;gap:6px;margin:10px 0 8px}
.pill{flex:1;font-size:11.5px;color:var(--muted);background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:5px 7px;white-space:nowrap;overflow:hidden}
.pill b{display:inline-block;min-width:18px;color:var(--muted);margin-right:4px}
.pill.done{color:#9fb0c0;border-color:#24502f}.pill.done b{color:var(--ok)}
.pill.cur{background:#132a4a;border-color:var(--llm);color:#fff}.pill.cur b{color:var(--llm)}
.pill.skip{opacity:.3;text-decoration:line-through}
.stagehead{display:flex;align-items:baseline;gap:12px;margin:2px 0 8px}
.stagehead h2{white-space:nowrap;margin:0;font-size:21px;font-family:'JetBrains Mono',monospace;font-weight:600}
.desc{color:var(--muted);font-size:13.5px}
.badge{font-size:11px;font-weight:700;padding:3px 8px;border-radius:5px;text-transform:uppercase;letter-spacing:.05em}
.badge.llm{background:#132a4a;color:var(--llm)}.badge.dafny{background:#12361f;color:var(--ok)}
.cols{display:grid;grid-template-columns:1fr 1.12fr;gap:16px}
.col{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 12px;height:470px;overflow:hidden;position:relative}
.ptitle{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between;gap:8px}
.ptitle code{text-transform:none;letter-spacing:0;color:var(--text);font-size:11.5px}
.time{color:var(--warn);text-transform:none;letter-spacing:0}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;align-items:center}
.chip{font-size:11.5px;padding:2px 8px;border-radius:10px}.chip.y{background:#12361f;color:var(--ok)}
.chip.n{background:#3d1416;color:#ff9b95}.sep{font-size:11px;color:var(--muted);margin-left:4px}
.cap{font-size:11.5px;color:var(--muted);margin:2px 0 4px}
pre.code{font-variant-ligatures:none;font-feature-settings:'liga' 0,'calt' 0;margin:0;background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:8px 10px;font:12.5px/1.42 'JetBrains Mono',monospace;white-space:pre-wrap;word-break:break-word;overflow:hidden}
pre.code.small{font-size:11.2px;line-height:1.36}
pre.out{font-size:11.2px;line-height:1.35;max-height:340px}pre.okout{border-color:#24502f}pre.badout{border-color:#6e2b28}
.k{color:#ff7b72}.s{color:#a5d6ff}.n{color:#79c0ff}.c{color:#7d8590}.c.tag{color:#d2a8ff}
.add{background:#12361f;display:inline-block;width:100%}
blockquote{margin:0 0 8px;padding:6px 10px;border-left:3px solid var(--llm);background:var(--panel2);font-size:13px;line-height:1.45;color:#cdd9e5;border-radius:0 6px 6px 0}
blockquote code,.lead code,.post code,.vsub code,.armwhat code,.rules code,.desc code{font-family:'JetBrains Mono',monospace;font-size:.92em;color:#ffd8a8}
.legend{font-size:11.5px;color:var(--muted);margin-top:6px}.addk{background:#12361f;color:var(--ok);padding:0 4px;border-radius:3px}
.waiting{position:absolute;inset:40px 12px 12px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;color:var(--muted)}
.waiting code{font-size:12px;color:var(--llm)}
.spinner{width:42px;height:42px;border-radius:50%;border:4px solid #1f3b63;border-top-color:var(--llm)}
.checks{display:flex;gap:8px;margin-top:9px;flex-wrap:wrap}
.chk{font-size:12.5px;padding:5px 10px;border-radius:7px;background:var(--panel);border:1px solid var(--border)}
.chk.ok{border-color:#24502f}.chk.ok:first-letter{color:var(--ok)}.chk.bad{border-color:#6e2b28;color:#ffb3ad}
.chk code{font-size:11.5px}
.bigcheck{margin-top:12px;font-size:16px;font-weight:600;padding:10px 14px;border-radius:8px}
.bigcheck.ok{background:#12361f;color:var(--ok)}.bigcheck.bad{background:#3d1416;color:#ff9b95}
.title{padding:46px 40px 0}.kicker{color:var(--acc);font-size:14px;font-weight:600;letter-spacing:.04em}
h1{font-size:40px;margin:8px 0 10px}.h1s{font-size:32px;margin-top:0}
.lead{font-size:16.5px;line-height:1.5;color:#cdd9e5;max-width:1120px}.lead.small{font-size:13px;color:var(--muted)}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:10px}
.settings{display:flex;gap:10px;flex-wrap:wrap;margin-top:44px}.settings span{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13.5px;color:var(--muted)}.settings b{color:var(--text);font-weight:600}
.post{margin-top:14px;font-size:15px;color:var(--muted)}.post code{font-size:14px}
.flow{display:flex;align-items:center;gap:8px;margin:30px 0 8px}
.fbox{flex:1;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px 10px;text-align:center;font-size:16px}
.fbox b{display:block;color:var(--llm);font-size:22px}.fbox small{color:var(--muted);font-size:12.5px}
.fbox.spec{border-color:var(--warn)}.fbox.proof{border-color:var(--ok)}.arrow{color:var(--muted);font-size:22px}
.blind{color:var(--warn);font-size:13px;margin-left:8px}
.rules{font-size:16px;line-height:1.6;color:#cdd9e5;margin-top:26px}
.armwrap{padding:60px 50px}.armtitle{font-size:26px}.armtitle .act{font-size:22px;padding:4px 14px}.armtitle .arm{font-size:18px}
.armwhat{font-size:19px;color:#cdd9e5;margin:22px 0}
.verdictwrap{display:grid;grid-template-columns:420px 1fr;gap:24px;padding-top:22px}
.vbig{font-size:64px;font-weight:800;letter-spacing:.02em;margin-top:30px}.vbig.verified,.vbig.refuted{color:var(--ok)}
.vbig.refuted{color:#ffa657}.vbig.abstain{color:var(--muted)}
.vsub{font-size:17px;color:#cdd9e5;margin:10px 0 18px;line-height:1.45}.wit{font-size:16px;margin-bottom:14px}
.wit code{font-family:'JetBrains Mono',monospace;color:#ffa657;font-size:16px}
.label{font-size:15px;padding:8px 12px;border-radius:8px;display:inline-block}.label.ok{background:#12361f;color:var(--ok)}
.label.bad{background:#3d1416;color:var(--bad)}.vtime{color:var(--muted);margin-top:18px;font-size:13px}
.note{padding:120px 80px;font-size:24px;line-height:1.5;color:#cdd9e5}
table.sum{border-collapse:collapse;font-size:15px;margin:18px 0;width:100%}
table.sum td,table.sum th{border-bottom:1px solid var(--border);padding:8px 10px;text-align:left}
table.sum th{color:var(--muted);font-weight:500}.v.verified{color:var(--ok)}.v.refuted{color:#ffa657}.v.abstain{color:var(--muted)}
.stats{display:flex;gap:14px;margin:18px 0}.stats div{flex:1;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.stats b{display:block;font-size:30px}.stats span{color:var(--muted);font-size:13px}
[data-phase],[data-only-phase]{visibility:hidden}
.print .slide{display:block;page-break-after:always;break-after:page}
@page{size:1280px 720px;margin:0}
pre,pre *,code,code *{font-variant-ligatures:none!important;font-feature-settings:"liga" 0,"calt" 0,"dlig" 0!important}
"""

JS = r"""
function show(){
  const print = location.search.includes('print');
  const slides=[...document.querySelectorAll('.slide')];
  if(print){document.body.classList.add('print');
    slides.forEach(s=>{s.querySelectorAll('[data-phase]').forEach(e=>e.style.visibility='visible');
                       s.querySelectorAll('[data-only-phase]').forEach(e=>e.style.display='none');});return;}
  const h=new URLSearchParams(location.hash.slice(1)); const n=+(h.get('s')||0), p=+(h.get('p')||9);
  slides.forEach((s,i)=>s.classList.toggle('on',i===n));
  const s=slides[n]; if(!s) return;
  s.querySelectorAll('[data-phase]').forEach(e=>e.style.visibility=(+e.dataset.phase<=p)?'visible':'hidden');
  s.querySelectorAll('[data-only-phase]').forEach(e=>e.style.visibility=(+e.dataset.onlyPhase===p)?'visible':'hidden');
}
window.addEventListener('hashchange',show);
document.addEventListener('keydown',e=>{const h=new URLSearchParams(location.hash.slice(1));let n=+(h.get('s')||0);
  const N=document.querySelectorAll('.slide').length; if(e.key==='ArrowRight')n=Math.min(N-1,n+1); if(e.key==='ArrowLeft')n=Math.max(0,n-1);
  location.hash='s='+n+'&p=9';});
show();
"""


def main():
    run, out = Path(sys.argv[1]), Path(sys.argv[2])
    slides, entries, totals, recs = build(run)
    bodies: list[tuple[str, int, float]] = []   # (html, phases, seconds per phase hint)
    for t in title_slides(entries, recs, totals):
        bodies.append((t, 1, 0))
    for s in slides:
        if s["type"] == "llm":
            inner, phases = llm_slide(s, entries), 3
        elif s["type"] == "verifier":
            inner, phases = verifier_slide(s), 2
        elif s["type"] == "verdict":
            inner, phases = verdict_slide(s, entries), 1
        elif s["type"] == "arm":
            inner, phases = arm_slide(s, entries), 1
        else:
            inner, phases = f'<div class="note">{html.escape(s["text"])}</div>', 1
        tr = tracker(s) if s["type"] in ("llm", "verifier", "verdict") else ""
        bodies.append((("{HEADER}" + tr + inner), phases, 0))
    bodies.append((summary_slide(slides, totals, recs), 1, 0))
    meta_slides = [None, None] + slides + [None]
    parts = []
    for n, (b, phases, _) in enumerate(bodies):
        s = meta_slides[n]
        b = b.replace("{HEADER}", header(s, n + 1, len(bodies)) if s else "")
        kind = s["type"] if s else "title"
        parts.append(f'<section class="slide" data-phases="{phases}" data-kind="{kind}">{b}</section>')
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><title>NFV demo: HumanEval/42</title>"
           f"<style>{CSS}</style></head><body>{''.join(parts)}<script>{JS}</script></body></html>")
    out.write_text(doc)
    manifest = [{"slide": n, "phases": p, "kind": (meta_slides[n] or {}).get("type", "title")}
                for n, (_, p, _) in enumerate(bodies)]
    (out.parent / "slides.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote {out}: {len(bodies)} slides, {sum(p for _, p, _ in bodies)} frames")


if __name__ == "__main__":
    main()

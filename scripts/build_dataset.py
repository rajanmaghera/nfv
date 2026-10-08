#!/usr/bin/env python3
"""Reconstruct nl2post-safe (206 entries) from the upstream nl2postcond artifacts.

Paper Section 2.1 / Appendix A.1: the corpus is pinned by Table 7, which gives,
for each of the 103 tasks, the postcondition response number ``p`` (in the
GPT-4 SimplePromptNoRef arm) and the mutant response number ``m`` (in
``distinct_code_mutants.jsonl``). The non-deterministic filtering steps (100
LLM-generated inputs per task, etc.) do not need to be re-run; we only:

  1. download the two upstream archives (cached under ``--cache``),
  2. pull the reference (``<entry>_original`` in raw_preprocessed_samples),
     the postcondition (summary_eval_results) and the mutant,
  3. sanitize the postcondition (drop comments, assertion messages and unused
     imports, then ``ast.unparse``) and check it against Table 7,
  4. optionally (``--validate``) execute each pair: the reference must satisfy
     S on the mutant's killing inputs and the mutant must violate it on at
     least one of them.

Output: data/nl2post_safe.jsonl (one Entry per line, references then mutants
interleaved per task).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfv.dataset import CORRECT, INCORRECT, Entry  # noqa: E402
from nfv.tiers import classify_tier  # noqa: E402

UPSTREAM = ("https://raw.githubusercontent.com/microsoft/intent-formalization/main/"
            "nl2postcondition-fse2024")
POST_ZIP = UPSTREAM + "/GeneratedPostconditions/EvalPlus/GPT4/SimplePromptNoRef/gpt4_simple_no_ref.zip"
MUT_ZIP = UPSTREAM + "/nl2postcondition_source_evalplus/code_mutants/distinct_code_mutants.jsonl.zip"
PINS = ROOT / "data" / "paper_table7_pins.json"
OUT = ROOT / "data" / "nl2post_safe.jsonl"
MAX_KILLING_INPUTS = 20


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"downloading {url}", file=sys.stderr)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
    return dest


def read_jsonl_from_zip(zpath: Path, member_suffix: str):
    with zipfile.ZipFile(zpath) as z:
        name = next(n for n in z.namelist() if n.endswith(member_suffix))
        with z.open(name) as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


# ---------------------------------------------------------------- sanitization


def _used_names(nodes) -> set[str]:
    return {n.id for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name)}


def sanitize_postcondition(text: str) -> str:
    """Comments and assert messages dropped, unused imports dropped, unparsed."""
    tree = ast.parse(text)
    stmts = []
    for node in tree.body:
        if isinstance(node, ast.Assert):
            node.msg = None
        stmts.append(node)
    non_imports = [s for s in stmts if not isinstance(s, (ast.Import, ast.ImportFrom))]
    used = _used_names(non_imports)
    kept = []
    for s in stmts:
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            names = [a for a in s.names if (a.asname or a.name).split(".")[0] in used]
            if not names:
                continue
            s.names = names
        kept.append(s)
    return "\n".join(ast.unparse(s) for s in kept)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------- programs


def extract_reference(solution: str, entry_point: str) -> str:
    """The upstream sample is `<entry>_original` + a wrapper asserting the
    postcondition; keep everything before the wrapper and drop the suffix."""
    tree = ast.parse(solution)
    lines = solution.splitlines()
    wrapper = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == entry_point)
    start = wrapper.lineno - 1
    if wrapper.decorator_list:
        start = wrapper.decorator_list[0].lineno - 1
    kept = "\n".join(lines[:start]).rstrip() + "\n"
    kept = re.sub(rf"\b{re.escape(entry_point)}_original\b", entry_point, kept)
    return kept.lstrip("\n")


def clean_source(src: str) -> str:
    return src.strip("\n") + "\n"


def docstring_of(src: str, entry_point: str) -> str:
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == entry_point:
            return ast.get_docstring(n) or ""
    raise ValueError(f"{entry_point} not in source")


# ---------------------------------------------------------------- validation

_RUNNER = r"""
import json, sys, math
src, post, entry, inputs = json.load(sys.stdin)
ns = {}
exec(src, ns)
fn = ns[entry]
import inspect
params = list(inspect.signature(fn).parameters)
out = []
for args in inputs:
    import copy
    a = copy.deepcopy(args)
    try:
        rv = fn(*copy.deepcopy(args))
    except Exception as e:
        out.append("exc:" + type(e).__name__); continue
    env = dict(zip(params, a)); env["return_value"] = rv
    try:
        exec(post, dict(env))
        out.append("pass")
    except AssertionError:
        out.append("fail")
    except Exception as e:
        out.append("perr:" + type(e).__name__)
print(json.dumps(out))
"""


def run_spec(src: str, post: str, entry: str, inputs: list, timeout: int = 60) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        runner = Path(d) / "runner.py"
        runner.write_text(_RUNNER)
        proc = subprocess.run([sys.executable, "-I", str(runner)], cwd=d,
                              input=json.dumps([src, post, entry, inputs]),
                              capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return json.loads(proc.stdout)


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache" / "upstream")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--validate", action="store_true",
                    help="execute reference/mutant on killing inputs (subprocess, -I)")
    args = ap.parse_args(argv)

    pins = json.loads(PINS.read_text())
    want_p = {(r["task_id"], r["p"]) for r in pins}
    want_m = {(r["task_id"], r["m"]) for r in pins}

    post_zip = fetch(POST_ZIP, args.cache / "gpt4_simple_no_ref.zip")
    mut_zip = fetch(MUT_ZIP, args.cache / "distinct_code_mutants.jsonl.zip")

    summary = {(r["task_id"], r["response_num"]): r
               for r in read_jsonl_from_zip(post_zip, "summary_eval_results.jsonl")
               if (r["task_id"], r["response_num"]) in want_p}
    samples = {(r["task_id"], r["response_num"]): r
               for r in read_jsonl_from_zip(post_zip, "raw_preprocessed_samples.jsonl")
               if (r["task_id"], r["response_num"]) in want_p}
    mutants = {(r["task_id"], r["response_num"]): r
               for r in read_jsonl_from_zip(mut_zip, "distinct_code_mutants.jsonl")
               if (r["task_id"], r["response_num"]) in want_m}

    entries: list[Entry] = []
    problems = []
    for pin in pins:
        tid, ep = pin["task_id"], pin["entry_point"]
        s = summary[(tid, pin["p"])]
        post = sanitize_postcondition(s["post_condition"])
        if _norm(post) != _norm(pin["postcondition"]):
            problems.append(f"{tid}: postcondition mismatch\n  upstream: {post}\n  paper:    {pin['postcondition']}")
        assert s["test_set_correct"], f"{tid}: upstream marks postcondition unsound"
        ref_src = clean_source(extract_reference(samples[(tid, pin["p"])]["solution"], ep))
        mut = mutants[(tid, pin["m"])]
        mut_src = clean_source(mut["solution"])
        kill = mut.get("unique_bopi") or []
        tnum = tid.split("/")[1]
        tier = classify_tier(ref_src, ep)  # tiers are per task (Table 1), from the reference
        for kind, src, label in (("reference", ref_src, CORRECT), ("mutant", mut_src, INCORRECT)):
            entries.append(Entry(
                id=f"HumanEval_{tnum}/{kind}", task_id=tid, entry_point=ep, kind=kind, label=label,
                source=src, docstring=docstring_of(src, ep), postcondition=post,
                tier=tier, p=pin["p"], m=pin["m"],
                killing_inputs=kill[:MAX_KILLING_INPUTS] if kind == "mutant" else [],
            ))

    if args.validate:
        for ref, mut in zip(entries[0::2], entries[1::2]):
            inputs = (mut_kill := [i for i in mutants[(mut.task_id, mut.m)]["unique_bopi"]])
            try:
                r_ref = run_spec(ref.source, ref.postcondition, ref.entry_point, inputs)
                r_mut = run_spec(mut.source, mut.postcondition, mut.entry_point, mut_kill)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{ref.task_id}: validation crashed: {e}")
                continue
            if any(r != "pass" for r in r_ref):
                problems.append(f"{ref.task_id}: reference does not pass S on killing inputs: {set(r_ref)}")
            if "fail" not in r_mut:
                problems.append(f"{mut.task_id}: mutant not killed by S on its inputs: {set(r_mut)}")
            # keep a killing input that actually fails the spec first in the list
            fails = [i for i, r in zip(mut_kill, r_mut) if r == "fail"]
            mut.killing_inputs = (fails + [i for i in mut.killing_inputs if i not in fails])[:MAX_KILLING_INPUTS]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e.to_json()) + "\n")

    tiers: dict[str, int] = {}
    for e in entries[0::2]:
        tiers[e.tier] = tiers.get(e.tier, 0) + 1
    print(f"wrote {len(entries)} entries over {len(pins)} tasks to {args.out}")
    print("tasks per tier:", dict(sorted(tiers.items())), "(paper: L0 21, L1 22, L2a 25, L2b 26, L3 9)")
    for p in problems:
        print("WARNING:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

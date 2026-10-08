"""`python -m nfv run ... --backend claude` end to end, with a fake `claude` executable
standing in for `claude -p` (the real CLI is never invoked)."""

import json
import os
import subprocess
import sys

import pytest
from conftest import ROOT
from test_llm_backends import fake_claude


def nfv(args, env, cwd=ROOT):
    full = {**os.environ, **env, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "nfv", *args], capture_output=True, text=True, env=full, cwd=cwd,
                          timeout=900)


def test_judge_arm_via_claude_cli(tmp_path):
    exe, env = fake_claude(tmp_path)
    env["NFV_CLAUDE_BIN"] = exe
    out = tmp_path / "run"
    p = nfv(["run", "--arm", "judge", "--backend", "claude", "--model", "haiku", "--tasks", "53", "--out", str(out)],
            env)
    assert p.returncode == 0, p.stderr
    res = {r["entry_id"]: r["verdict"] for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
    assert res == {"HumanEval_53/reference": "verified", "HumanEval_53/mutant": "refuted"}
    assert "judge" in p.stdout and "100.0" in p.stdout          # 2/2 correct
    calls = (tmp_path / "argv.jsonl").read_text().splitlines()
    assert len(calls) == 2 and all("--model" in json.loads(c)["argv"] for c in calls)
    transcript = [json.loads(l) for l in (out / "transcript.jsonl").read_text().splitlines()]
    assert {t["entry_id"] for t in transcript} == set(res) and all(t["stage"] == "judge" for t in transcript)
    # resuming skips finished entries
    p2 = nfv(["run", "--arm", "judge", "--backend", "claude", "--tasks", "53", "--out", str(out)], env)
    assert "0 entries to run (2 already done)" in p2.stderr
    assert len((tmp_path / "argv.jsonl").read_text().splitlines()) == 2


@pytest.mark.usefixtures("dafny")
def test_pipeline_arm_via_claude_cli_and_report(tmp_path):
    exe, env = fake_claude(tmp_path, ["truncate_mut"])
    env["NFV_CLAUDE_BIN"] = exe
    out = tmp_path / "run"
    p = nfv(["run", "--arm", "pipeline", "--backend", "claude", "--ids", "HumanEval_2/mutant", "--attempts", "1",
             "--rounds", "1", "--out", str(out)], env)
    assert p.returncode == 0, p.stderr
    recs = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines()]
    assert [(r["arm"], r["verdict"]) for r in recs] == [("refute", "refuted")]
    proved = out / "artifacts" / "HumanEval_2__mutant" / "refute" / "attempt_1" / "4_refute_proved.dfy"
    assert "requires number == 3.5" in proved.read_text()
    rep = nfv(["report", str(out)], {})
    assert rep.returncode == 0 and "refute" in rep.stdout and "Table 4" in rep.stdout


def test_cli_offline_commands():
    p = nfv(["prompt", "theory", "HumanEval_133/reference"], {})
    assert p.returncode == 0 and "[NFV-STAGE: dafny.theory]" in p.stdout and "math.ceil" in p.stdout
    assert "isinstance(return_value, int)" not in p.stdout       # stage 1a never sees S_L
    p = nfv(["show", "HumanEval_2/mutant"], {})
    assert json.loads(p.stdout)["label"] == "incorrect"
    p = nfv(["check"], {})
    assert p.returncode == 0 and "206 entries" in p.stdout

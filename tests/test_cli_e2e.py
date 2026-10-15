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


def test_usage_guard_stops_starting_entries(tmp_path):
    exe, env = fake_claude(tmp_path)
    env.update(NFV_CLAUDE_BIN=exe, FAKE_CLAUDE_UTILIZATION="0.10,0.20,0.76,0.77")
    out = tmp_path / "run"
    p = nfv(["run", "--arm", "judge", "--backend", "claude", "--tasks", "53,2,42", "--jobs", "1",
             "--stop-at-usage", "0.75", "--out", str(out)], env)
    assert p.returncode == 0, p.stderr
    recs = (out / "results.jsonl").read_text().splitlines()
    assert len(recs) == 3                          # third call reported 76%: nothing new started after it
    assert "not starting the remaining 3 entries" in p.stderr
    assert "five_hour 76%" in p.stderr


@pytest.mark.usefixtures("dafny")
def test_usage_guard_aborts_in_flight_entries_unrecorded(tmp_path):
    exe, env = fake_claude(tmp_path, ["truncate_mut"])
    # call 1 (stage 1a) reports 10%, call 2 (stage 2) reports 90% > 75% + 3% slack -> call 3 is refused
    env.update(NFV_CLAUDE_BIN=exe, FAKE_CLAUDE_UTILIZATION="0.10,0.90")
    out = tmp_path / "run"
    p = nfv(["run", "--arm", "pipeline", "--backend", "claude", "--ids", "HumanEval_2/mutant,HumanEval_2/reference",
             "--jobs", "1", "--stop-at-usage", "0.75", "--out", str(out)], env)
    assert p.returncode == 0, p.stderr
    assert "ABORTED (not recorded)" in p.stderr and "not starting the remaining 1 entries" in p.stderr
    assert not (out / "results.jsonl").exists() or (out / "results.jsonl").read_text() == ""
    assert len((tmp_path / "argv.jsonl").read_text().splitlines()) == 2   # no call after the limit


@pytest.mark.usefixtures("dafny")
def test_trace_records_llm_calls_verifier_runs_and_events(tmp_path):
    exe, env = fake_claude(tmp_path, ["truncate_mut"])
    env["NFV_CLAUDE_BIN"] = exe
    out = tmp_path / "run"
    p = nfv(["run", "--arm", "pipeline", "--backend", "claude", "--ids", "HumanEval_2/mutant", "--attempts", "1",
             "--rounds", "1", "--trace", "--out", str(out)], env)
    assert p.returncode == 0, p.stderr
    recs = [json.loads(l) for l in (out / "trace.jsonl").read_text().splitlines()]
    kinds = [r["kind"] for r in recs]
    assert kinds[0] == "arm-start" and kinds[-1] == "arm-end" and recs[-1]["verdict"] == "refuted"
    llm = [r for r in recs if r["kind"] == "llm"]
    assert [r["stage"] for r in llm] == ["dafny.theory", "dafny.translate", "dafny.precondition", "dafny.spec",
                                         "dafny.witness"]
    ver = [r for r in recs if r["kind"] == "verifier"]
    assert ver and all("artifact.dfy" in r["command"] and r["input"] for r in ver)
    assert all(r["entry_id"] == "HumanEval_2/mutant" for r in recs)
    assert [r["seq"] for r in recs] == list(range(1, len(recs) + 1))

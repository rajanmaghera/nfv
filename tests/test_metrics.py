"""Scoring reproduces the paper's own arithmetic (Tables 3 and 6)."""

import pytest

from nfv.metrics import compose_pipeline, failure_table, main_table, score, wrong_verdicts


def mk(arm, n_correct_label, n_incorrect_label, verdict_correct_label, verdict_incorrect_label):
    """Synthetic results: entries with each label, the first k of which get the given verdict."""
    out = []
    for i in range(n_correct_label):
        v = verdict_correct_label[i] if i < len(verdict_correct_label) else "abstain"
        out.append({"entry_id": f"T{i}/reference", "arm": arm, "label": "correct", "verdict": v})
    for i in range(n_incorrect_label):
        v = verdict_incorrect_label[i] if i < len(verdict_incorrect_label) else "abstain"
        out.append({"entry_id": f"T{i}/mutant", "arm": arm, "label": "incorrect", "verdict": v})
    return out


def test_verification_row_of_table3():
    # 72 of 103 references proved, 5 mutants proved: precision 93.5, recall 69.9, F1 0.80
    rs = mk("verify", 103, 103, ["verified"] * 72, ["verified"] * 5)
    s = score("verify", rs, "verified")
    assert (s.population, s.issued, s.wrong) == (103, 77, 5)
    assert s.precision == pytest.approx(0.935, abs=5e-4)
    assert s.recall == pytest.approx(0.699, abs=5e-4)
    assert round(s.f1, 2) == 0.80


def test_refutation_row_of_table3():
    rs = mk("refute", 103, 103, ["refuted"] * 6, ["refuted"] * 47)
    s = score("refute", rs, "refuted")
    assert (s.issued, s.wrong) == (53, 6)
    assert round(100 * s.precision, 1) == 88.7 and round(100 * s.recall, 1) == 45.6 and round(s.f1, 2) == 0.60


def test_pipeline_composition_matches_table3():
    # refutation issues 53 (47 mutants, 6 references); verification, on what refutation left open, proves 71
    # references and 4 mutants -> 128 issued, 10 wrong, 118 right: 92.2% precision, 57.3% recall, F1 0.71
    refute = mk("refute", 103, 103, ["refuted"] * 6, ["refuted"] * 47)
    verify = mk("verify", 103, 103, ["abstain"] * 6 + ["verified"] * 71, ["abstain"] * 47 + ["verified"] * 4)
    s = score("pipeline", compose_pipeline(refute, verify), None)
    assert (s.population, s.issued, s.wrong) == (206, 128, 10)
    assert (round(100 * s.precision, 1), round(100 * s.recall, 1), round(s.f1, 2)) == (92.2, 57.3, 0.71)


def test_pipeline_prefers_refutation_when_both_arms_answer():
    refute = mk("refute", 1, 1, [], ["refuted"])
    verify = mk("verify", 1, 1, ["verified"], ["verified"])
    out = {r["entry_id"]: r["verdict"] for r in compose_pipeline(refute, verify)}
    assert out == {"T0/reference": "verified", "T0/mutant": "refuted"}


def test_judge_scores_both_verdicts_and_never_abstains():
    # LLM-as-judge in the paper: rejects 56/103 correct programs, accepts 2/103 incorrect -> 58 wrong, 72% precision
    rs = mk("judge", 103, 103, ["refuted"] * 56 + ["verified"] * 47, ["verified"] * 2 + ["refuted"] * 101)
    s = score("judge", rs, None)
    assert (s.issued, s.wrong) == (206, 58)
    assert round(100 * s.precision) == 72


def test_reports_render():
    rs = mk("verify", 3, 3, ["verified"], ["verified"]) + mk("refute", 3, 3, [], ["refuted"])
    for r in rs:
        r["failures"] = ["type"] if r["verdict"] == "abstain" else []
    text = main_table(rs)
    assert "pipeline (refute, then verify)" in text and "verify" in text
    assert "T0/mutant" in wrong_verdicts(rs)
    assert "type" in failure_table(rs)

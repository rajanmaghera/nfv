"""The bounded arm end to end: sample LLM outputs -> real CBMC."""

from conftest import dataset_entry, scripted

from nfv.cbmc_pipeline import check_harness, refutation_arm
from nfv.dafny_pipeline import ABSTAIN, REFUTED, Budget
from nfv.extract import extract_code
from nfv.llm import ScriptedBackend


def test_mutant_refuted_after_goal_blind_ub_repair(cbmc):
    llm = scripted("cbmc_truncate_mut")
    r = refutation_arm(dataset_entry("HumanEval_2/mutant"), llm, cbmc, Budget(attempts=1, rounds=2))
    assert r.verdict == REFUTED, r.events
    statuses = [e["status"] for e in r.events if e["stage"] == "cbmc"]
    assert statuses == ["undefined-behaviour", "counterexample"]
    ub = next(e for e in r.events if e["status"] == "undefined-behaviour")
    assert "float to signed integer" in ub["detail"]
    # the UB repair went to the translation stage and its prompt never contained the postcondition
    repair = [p for s, p in llm.calls if s == "c.translate"][-1]
    assert "CBMC found undefined behaviour" in repair and "0 <= return_value < 1" not in repair
    assert any(w.startswith("number = ") for w in r.witness)


def test_reference_gets_no_counterexample(cbmc):
    r = refutation_arm(dataset_entry("HumanEval_2/reference"), scripted("cbmc_truncate_ref"), cbmc,
                       Budget(attempts=1, rounds=1))
    assert r.verdict == ABSTAIN and r.failures == ["no-counterexample"] and r.reached_proof


def test_harness_audit():
    good = extract_code(scripted("cbmc_truncate_mut")._queues["c.translate"][1], ("c",))
    assert check_harness(good, "truncate_number") == []
    assert check_harness(good.replace('"nfv_spec"', '"x"'), "truncate_number")
    assert check_harness(good.replace("__CPROVER_assume(nfv_pre(number));", ""), "truncate_number")
    sneaky = good.replace("return decimal_part + 1;", "if (nondet_double() > 0) return 0.5;\n  return decimal_part + 1;")
    assert any("deterministic" in e for e in check_harness(sneaky, "truncate_number"))


def test_compile_errors_are_repaired_or_fail_the_attempt(cbmc):
    s = scripted("cbmc_truncate_ref")
    s._queues["c.precondition"] = ["```c\nbool nfv_pre(double number) { return number > 0.0 }\n```"]  # missing ;
    r = refutation_arm(dataset_entry("HumanEval_2/reference"), s, cbmc, Budget(attempts=1, rounds=0, stage_rounds=1))
    assert r.verdict == ABSTAIN and r.failures == ["type"]


def test_cbmc_llm_error(cbmc):
    r = refutation_arm(dataset_entry("HumanEval_2/reference"), ScriptedBackend({}), cbmc, Budget(attempts=1))
    assert r.failures == ["llm-error"]


def test_saturating_library_model_gives_spurious_refutation(cbmc):
    # observed with opus in the pilot: int(2.68e154) saturated to LLONG_MAX -> a false counterexample
    r = refutation_arm(dataset_entry("HumanEval_2/reference"), scripted("cbmc_truncate_ref_saturating"), cbmc,
                       Budget(attempts=1, rounds=1))
    assert r.verdict == REFUTED and r.label == "correct"


def test_model_domain_guard_removes_spurious_refutation(cbmc):
    r = refutation_arm(dataset_entry("HumanEval_2/reference"), scripted("cbmc_truncate_ref_domain"), cbmc,
                       Budget(attempts=1, rounds=1))
    assert r.verdict == ABSTAIN and r.failures == ["no-counterexample"]

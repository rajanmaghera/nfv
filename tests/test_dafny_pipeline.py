"""The proof-based arm end to end: sample LLM outputs -> real Dafny.

Scenarios:
* the paper's running example (Section 1.2): repair loops in stages 2 and 1b, a cheating proof
  rejected by the freeze, then the paper's two lemmas -> verified;
* its buggy variant: witness l == [0, 0] -> refuted, reachability holds;
* HumanEval/2 (dataset entries) through the combined pipeline: reference -> refutation abstains,
  verification proves it; mutant -> refuted with number == 3.5;
* a proof search that only ever cheats -> abstain, with the audit failures recorded;
* a vacuous witness (contradicting the precondition) -> reachability check blocks the refutation.
"""

import pytest
from conftest import dataset_entry, paper_example, scripted

from nfv import dafny_artifact as A
from nfv.dafny_pipeline import (ABSTAIN, REFUTED, VERIFIED, Budget, pipeline, refutation_arm,
                                verification_arm)
from nfv.llm import ScriptedBackend


def stages(result):
    return [(e["stage"], e["status"]) for e in result.events]


def test_paper_example_verified_after_repairs(dafny, tmp_path):
    llm = scripted("shifted_mean")
    r = verification_arm(paper_example(), llm, dafny, Budget(attempts=1, rounds=3), artifacts_dir=tmp_path)
    assert r.verdict == VERIFIED, r.events
    ev = stages(r)
    assert ("2.translate", "audit") in ev            # untagged code + misquote sent back for repair
    assert ("1b.precondition", "audit") in ev        # unsatisfiable precondition caught
    assert ("4.proof", "audit") in ev                # the code-rewriting "proof" was rejected
    assert ev[-1] == ("4.proof", "verified")
    assert "lemma ShiftedSum" in r.artifact and "lemma DivDistrib" in r.artifact
    # the stages never saw the postcondition before stage 3
    for stage, prompt in llm.calls:
        if stage in ("dafny.theory", "dafny.translate", "dafny.precondition"):
            assert "sum(l) / len(l) + 1" not in prompt, stage
    assert (tmp_path / "verify" / "attempt_1" / "4_verify_proved.dfy").exists()
    # the final artifact re-verifies independently
    assert dafny.verify(r.artifact).ok


def test_paper_example_bug_refuted(dafny):
    r = refutation_arm(paper_example(buggy=True), scripted("shifted_mean_bug"), dafny, Budget(attempts=1, rounds=2))
    assert r.verdict == REFUTED, r.events
    assert r.witness == ["l == [0.0, 0.0]"]
    assert ("reachability", "ok") in stages(r)
    prog = A.split_regions(r.artifact)["PROGRAM"]
    assert A.entry_contract(prog, "shifted_mean")["ensures"] == ["!((return_value == Sum(l) / (|l| as real) + 1.0))"]


def test_dataset_task_2_through_the_pipeline(dafny):
    ref = pipeline(dataset_entry("HumanEval_2/reference"), scripted("truncate_ref"), dafny,
                   Budget(attempts=1, rounds=1))
    assert ref["refute"].verdict == ABSTAIN and ref["verify"].verdict == VERIFIED
    assert ("front-end", "reused") in stages(ref["verify"])   # stages 1a/2/1b/3 shared byte-identically
    mut = pipeline(dataset_entry("HumanEval_2/mutant"), scripted("truncate_mut"), dafny, Budget(attempts=1, rounds=1))
    assert mut["refute"].verdict == REFUTED and "verify" not in mut
    assert mut["refute"].witness == ["number == 3.5"]


def test_cheating_proof_search_abstains(dafny):
    s = scripted("shifted_mean")
    s._queues["dafny.proof"] = [s._queues["dafny.proof"][0]]   # only the code-rewriting answer
    r = verification_arm(paper_example(), s, dafny, Budget(attempts=2, rounds=2))
    assert r.verdict == ABSTAIN
    assert r.failures == ["proof", "proof"] and r.reached_proof
    assert sum(1 for e in r.events if e["stage"] == "4.proof" and e["status"] == "audit") == 4


def test_vacuous_witness_fails_reachability(dafny):
    s = scripted("shifted_mean_bug")
    # empty list contradicts the frozen precondition |l| > 0: the negated goal "verifies" vacuously
    s._queues["dafny.witness"] = ["```dafny\nrequires l == []\n```"]
    r = refutation_arm(paper_example(buggy=True), s, dafny, Budget(attempts=1, rounds=0))
    assert r.verdict == ABSTAIN
    assert r.failures == ["reachability"]


def test_spec_may_not_reference_the_translation(dafny):
    s = scripted("shifted_mean")
    for stage in ("dafny.translate", "dafny.precondition"):  # skip straight to the good answers
        s._queues[stage] = s._queues[stage][1:]
    s._queues["dafny.spec"] = ["```dafny ensures\nensures return_value == Sum(Shift(l)) / (|l| as real)\n```"]
    r = verification_arm(paper_example(), s, dafny, Budget(attempts=1, rounds=0, stage_rounds=0))
    assert r.verdict == ABSTAIN and r.failures == ["audit"]
    assert any("may not refer to the translated program (Shift)" in e.get("detail", "") for e in r.events)


def test_llm_errors_are_recorded_not_raised(dafny):
    r = verification_arm(paper_example(), ScriptedBackend({}), dafny, Budget(attempts=2, rounds=0))
    assert r.verdict == ABSTAIN and r.failures == ["llm-error", "llm-error"]


@pytest.mark.parametrize("arm", [verification_arm, refutation_arm])
def test_unparseable_outputs_fail_the_attempt_cleanly(dafny, arm):
    r = arm(paper_example(), ScriptedBackend(lambda p: "I am not sure what you mean."), dafny,
            Budget(attempts=1, rounds=0, stage_rounds=1))
    assert r.verdict == ABSTAIN and r.failures in (["type"], ["audit"])

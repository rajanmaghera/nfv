"""Baselines of paper Section 4.5 on sample outputs."""

from conftest import dataset_entry, scenario

from nfv.baselines import llm_judge, unstaged
from nfv.dafny_pipeline import ABSTAIN, REFUTED, VERIFIED, Budget
from nfv.llm import ScriptedBackend


def test_judge_commits_on_every_entry():
    j = scenario("judge")["judge"]
    r = llm_judge(dataset_entry("HumanEval_53/mutant"), ScriptedBackend([j[0]]))
    assert r.verdict == REFUTED
    r = llm_judge(dataset_entry("HumanEval_53/reference"), ScriptedBackend([j[1]]))
    assert r.verdict == VERIFIED


def test_judge_prompt_supplies_docstring_assumptions():
    s = ScriptedBackend(["VERDICT: CORRECT"])
    llm_judge(dataset_entry("HumanEval_2/reference"), s)
    prompt = s.calls[0][1]
    assert "Given a positive floating point number" in prompt and "assert 0 <= return_value < 1" in prompt


def test_judge_retries_unparseable_answers():
    r = llm_judge(dataset_entry("HumanEval_53/reference"), ScriptedBackend(["hmm", "VERDICT: INCORRECT"]))
    assert r.verdict == REFUTED and r.attempts_used == 2


UNSTAGED_CHEAT = """The postcondition is easy to prove if the method just returns it:

```dafny
method add(x: int, y: int) returns (return_value: int)
  ensures return_value == x + y
{
  return_value := x + y;
}
```"""


def test_unstaged_proves_the_buggy_program(dafny):
    # nothing stops the unstaged agent from rewriting the program (paper: 57% of its unsound proofs)
    r = unstaged(dataset_entry("HumanEval_53/mutant"), ScriptedBackend([UNSTAGED_CHEAT]), dafny,
                 Budget(attempts=1, rounds=0))
    assert r.verdict == VERIFIED and r.label == "incorrect"


def test_unstaged_repair_loop_and_abstention(dafny):
    wrong = "```dafny\nmethod add(x: int, y: int) returns (return_value: int)\n  ensures return_value == x + y\n" \
            "{\n  return_value := x - y;\n}\n```"
    r = unstaged(dataset_entry("HumanEval_53/mutant"), ScriptedBackend([wrong, wrong]), dafny,
                 Budget(attempts=1, rounds=1))
    assert r.verdict == ABSTAIN and r.failures == ["proof"]
    assert sum(e["status"] == "failed" for e in r.events) == 2

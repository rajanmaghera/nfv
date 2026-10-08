"""The reconstructed nl2post-safe corpus matches the paper's description."""

import ast
import json
from collections import Counter

from conftest import ROOT

from nfv.dataset import CORRECT, INCORRECT, load_entries, numbered
from nfv.tiers import classify_tier


def test_corpus_shape():
    entries = load_entries()
    assert len(entries) == 206
    assert Counter(e.label for e in entries) == {CORRECT: 103, INCORRECT: 103}
    tasks = Counter(e.task_id for e in entries)
    assert len(tasks) == 103 and set(tasks.values()) == {2}
    assert len({e.id for e in entries}) == 206


def test_pairs_share_postcondition_and_tier():
    entries = load_entries()
    by_task = {}
    for e in entries:
        by_task.setdefault(e.task_id, []).append(e)
    for pair in by_task.values():
        ref, mut = sorted(pair, key=lambda e: e.kind != "reference")
        assert (ref.kind, mut.kind) == ("reference", "mutant")
        assert ref.postcondition == mut.postcondition
        assert ref.tier == mut.tier
        assert ref.source != mut.source
        assert mut.killing_inputs, f"{mut.id} has no killing input"


def test_postconditions_match_paper_table7():
    pins = {p["task_id"]: p for p in json.loads((ROOT / "data" / "paper_table7_pins.json").read_text())}
    norm = lambda s: "".join(s.split())  # noqa: E731
    for e in load_entries():
        assert norm(e.postcondition) == norm(pins[e.task_id]["postcondition"]), e.id
        assert (e.p, e.m) == (pins[e.task_id]["p"], pins[e.task_id]["m"])


def test_sources_parse_and_define_entry_point():
    for e in load_entries():
        tree = ast.parse(e.source)
        assert any(isinstance(n, ast.FunctionDef) and n.name == e.entry_point for n in tree.body), e.id
        assert "_original" not in e.source
        ast.parse(e.postcondition)
        assert "return_value" in e.postcondition
        assert e.signature().startswith(f"def {e.entry_point}(")


def test_tiers_of_paper_appendix_b_examples():
    # Appendix B gives one example per tier
    expect = {157: "L0", 121: "L1", 120: "L2a", 30: "L2b", 133: "L3"}
    for e in load_entries():
        if e.kind == "reference" and e.task_num in expect:
            assert e.tier == expect[e.task_num], (e.task_num, e.tier)


def test_tier_classifier_rules():
    assert classify_tier("def f(a, b):\n    return a + b\n") == "L0"
    assert classify_tier("def f(l):\n    return sum(l)\n") == "L1"
    assert classify_tier("def f(l):\n    return sorted(l)\n") == "L2a"
    assert classify_tier("def f(l):\n    return list(filter(lambda x: x > 0, l))\n") == "L2b"
    assert classify_tier("import math\ndef f(x):\n    return math.ceil(x)\n") == "L3"
    assert classify_tier("from typing import List\ndef f(l: List[int]):\n    return len(l)\n") == "L0"


def test_numbered_source_matches_src_tag_convention():
    for e in load_entries()[:10]:
        lines = numbered(e.source).splitlines()
        assert len(lines) == len(e.source.splitlines())
        assert lines[0].startswith("  1 | ") and lines[-1].startswith(f"{len(lines):3d} | ")

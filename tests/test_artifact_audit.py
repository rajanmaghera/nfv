"""The staged discipline's mechanical checks, on sample stage outputs (no verifier needed)."""

import pytest
from conftest import SHIFTED_MEAN, SHIFTED_MEAN_BUG, scenario

from nfv import dafny_artifact as A
from nfv.extract import extract_code

TRANSLATION = extract_code(scenario("shifted_mean")["dafny.translate"][1])
THEORY = extract_code(scenario("shifted_mean")["dafny.theory"][0])
GOOD_PROOF = extract_code(scenario("shifted_mean")["dafny.proof"][1])
CHEAT_PROOF = extract_code(scenario("shifted_mean")["dafny.proof"][0])


def frozen_artifact() -> str:
    prog = A.insert_contract(TRANSLATION, "shifted_mean", [(A.PRE_TAG, "|l| > 0")],
                             [(A.POST_TAG, "return_value == Sum(l) / (|l| as real) + 1.0")])
    return A.assemble(THEORY, "", prog)


def frozen() -> A.Frozen:
    return A.Frozen.of(frozen_artifact(), "shifted_mean")


# ---------------------------------------------------------------- provenance (stage 2)


def test_good_translation_passes_provenance():
    assert A.check_provenance(TRANSLATION, SHIFTED_MEAN) == []
    assert A.python_coverage(TRANSLATION, SHIFTED_MEAN, "shifted_mean") == (2, 2)  # def + return


def test_untagged_code_and_misquoted_source_are_rejected():
    bad = extract_code(scenario("shifted_mean")["dafny.translate"][0])
    errs = A.check_provenance(bad, SHIFTED_MEAN)
    assert any("untagged code" in e for e in errs)
    assert any("does not quote source.py:3 verbatim" in e for e in errs)


def test_src_tag_line_ranges_and_bounds():
    prog = "// src: source.py:2-3 | \"Average the list after adding 1 to each element; l must be non-empty.\" " \
           "return sum(x + 1 for x in l) / len(l)\nconst X := 1\n"
    assert A.check_provenance(prog, SHIFTED_MEAN) == []
    assert "out of range" in A.check_provenance("// src: source.py:9 | x\nconst X := 1\n", SHIFTED_MEAN)[0]
    assert "needs a kind" in A.check_provenance("// synth:\nconst X := 1\n", SHIFTED_MEAN)[0]


# ---------------------------------------------------------------- unproven assumptions


@pytest.mark.parametrize("snippet,needle", [
    ("method m() { assume false; }", "`assume`"),
    ("method m() { expect 1 == 2; }", "`expect`"),
    ("lemma {:axiom} L() ensures false", "{:axiom}"),
    ("lemma L() ensures false", "bodyless lemma"),
    ("method {:verify false} m() ensures false {}", "{:verify}"),
    ("method m() decreases * { }", "decreases *"),
    ("include \"x.dfy\"", "`include`"),
    ("method {:extern} m()", "{:extern}"),
    ("method {:only} m() {}", "{:only}"),
    ("function {:mystery} F(): int { 1 }", "allow-list"),
])
def test_forbidden_constructs(snippet, needle):
    errs = A.check_forbidden(snippet, region="PROGRAM")
    assert any(needle.strip("`{}") in e for e in errs), errs


def test_theory_may_hold_marked_axioms_only():
    assert A.check_forbidden("function {:axiom} PyCeil(x: real): int ensures PyCeil(x) as real >= x",
                             region="THEORY") == []
    assert A.check_forbidden("function PyCeil(x: real): int", region="THEORY")  # unmarked axiom
    assert A.check_forbidden("method M() {}", region="THEORY")                    # theory must be pure
    assert A.check_forbidden(THEORY, region="THEORY") == []


# ---------------------------------------------------------------- the freeze (stage 4)


def test_honest_proof_passes_the_freeze():
    assert A.audit_proof_artifact(frozen(), GOOD_PROOF, SHIFTED_MEAN) == []


def test_rewriting_executable_code_is_rejected():
    errs = A.audit_proof_artifact(frozen(), CHEAT_PROOF, SHIFTED_MEAN)
    assert any("executable code of method shifted_mean changed" in e for e in errs), errs


def _mutate(old: str, new: str) -> list[str]:
    art = GOOD_PROOF.replace(old, new, 1)
    assert art != GOOD_PROOF, f"{old!r} not found"
    return A.audit_proof_artifact(frozen(), art, SHIFTED_MEAN)


def test_weakened_goal_is_rejected():
    assert any("postcondition (goal)" in e for e in
               _mutate("ensures return_value == Sum(l) / (|l| as real) + 1.0", "ensures true"))


def test_strengthened_precondition_is_rejected():
    assert any("precondition" in e for e in _mutate("  requires |l| > 0\n", "  requires |l| > 0\n  requires |l| == 1\n"))


def test_edited_theory_is_rejected():
    assert any("THEORY" in e for e in _mutate("else l[0] + Sum(l[1..])", "else 1.0 + Sum(l[1..])"))


def test_assumption_in_proof_is_rejected():
    assert any("assume" in e for e in _mutate("  ShiftedSum(l); DivDistrib", "  assume false;\n  ShiftedSum(l); DivDistrib"))


def test_axiom_lemma_in_proof_is_rejected():
    errs = _mutate("// synth: proof, nonlinear arithmetic step",
                   "// synth: proof\nlemma {:axiom} Magic() ensures false\n// synth: proof, nonlinear arithmetic step")
    assert any("axiom" in e for e in errs)


def test_dropping_a_src_line_is_rejected():
    assert any("src:" in e for e in _mutate("  // src: source.py:3 | return sum(x + 1 for x in l) / len(l)\n", ""))


def test_new_executable_code_under_a_proof_tag_is_rejected():
    errs = _mutate("  ShiftedSum(l); DivDistrib(Sum(l), |l| as real);",
                   "  ShiftedSum(l); DivDistrib(Sum(l), |l| as real);\n  return_value := 0.0;")
    assert any("executable code" in e for e in errs)


def test_ghost_additions_are_erased():
    # asserts, ghost vars, calc, lemma calls, invariants and lemma declarations vanish under erasure
    base = ("method m(n: int) returns (r: int)\n{\n  r := 0;\n  var i := 0;\n  while i < n\n  {\n"
            "    i := i + 1;\n    r := r + 2;\n  }\n}\n")
    proof = ("lemma L(x: int) ensures x + 0 == x {}\n"
             "method m(n: int) returns (r: int)\n  decreases n\n{\n  r := 0;\n  var i := 0;\n  ghost var g := 3;\n"
             "  while i < n\n    invariant 0 <= i <= n || n < 0\n    invariant r == 2 * i\n    decreases n - i\n  {\n"
             "    assert r == 2 * i by { L(r); }\n    i := i + 1;\n    L(i);\n    r := r + 2;\n"
             "    calc { r; == { L(r); } r + 0; }\n  }\n  assert r >= 0 || n < 0;\n}\n")
    before, _ = A.ghost_erasure(base)
    after, errs = A.ghost_erasure(proof)
    assert errs == []
    assert before == after


# ---------------------------------------------------------------- refutation plumbing


def test_witness_contract_and_reachability_rewrite():
    prog = A.insert_contract(extract_code(scenario("shifted_mean_bug")["dafny.translate"][0]), "shifted_mean",
                             [(A.PRE_TAG, "|l| > 0"), (A.WITNESS_TAG, "l == [0.0, 0.0]")],
                             [(A.NEGATED_POST_TAG, "!((return_value == Sum(l) / (|l| as real) + 1.0))")])
    c = A.entry_contract(prog, "shifted_mean")
    assert c["requires"] == ["|l| > 0", "l == [0.0, 0.0]"]
    assert c["ensures"] == ["!((return_value == Sum(l) / (|l| as real) + 1.0))"]
    unreachable = A.replace_entry_ensures(prog, "shifted_mean", "false")
    assert A.entry_contract(unreachable, "shifted_mean")["ensures"] == ["false"]
    assert A.check_provenance(prog, SHIFTED_MEAN_BUG) == []
    # the refutation proof fixture (three unfolding asserts) passes the freeze
    art = A.assemble(THEORY, "", prog)
    proof = extract_code(scenario("shifted_mean_bug")["dafny.refute.proof"][0])
    assert A.audit_proof_artifact(A.Frozen.of(art, "shifted_mean"), proof, SHIFTED_MEAN_BUG) == []


def test_contract_insertion_requires_own_line_brace():
    with pytest.raises(A.ArtifactError, match="own line"):
        A.insert_contract("// synth: x\nmethod f(x: int) returns (return_value: int) {\n  return_value := x;\n}\n",
                          "f", [], [])
    with pytest.raises(A.ArtifactError, match="no `method g`"):
        A.entry_header(TRANSLATION, "g")
    assert A.entry_header(TRANSLATION, "shifted_mean") == "method shifted_mean(l: seq<real>) returns (return_value: real)"
    assert A.entry_params(TRANSLATION, "shifted_mean") == "l: seq<real>"


def test_region_markers_are_required():
    errs = A.audit_proof_artifact(frozen(), GOOD_PROOF.replace("// ==== NFV:SPEC END ====\n", ""), SHIFTED_MEAN)
    assert any("region markers" in e for e in errs)


def test_tokenizer_handles_strings_chars_comments_and_primes():
    toks = [t.text for t in A.tokenize("var s := \"a // not a comment\"; /* x /* nested */ y */ var c := 'b'; x' := x'';")]
    assert toks == ["var", "s", ":=", "\"a // not a comment\"", ";", "var", "c", ":=", "'b'", ";", "x'", ":=",
                    "x''", ";"]


def test_cardinality_before_body_brace_is_not_a_set_display():
    # regression: seen in the full Opus run, `|s| {` made bodies look absent (false "unproven assumption")
    code = ("function PyIndex(s: seq<int>, i: int): int\n  requires -|s| <= i < |s|\n"
            "{ if i < 0 then s[|s| + i] else s[i] }\n"
            "method m(xs: seq<int>) returns (r: seq<int>)\n  ensures |r| == |(set x | x in r)|\n{\n  r := [];\n}\n")
    decls = A.declarations(A.tokenize(code))
    assert [(d.kind, d.name, d.has_body) for d in decls] == [("function", "PyIndex", True), ("method", "m", True)]
    assert A.check_forbidden(code, region="PROGRAM") == []
    assert A.entry_contract(code, "m")["ensures"] == ["|r| == |(set x | x in r)|"]

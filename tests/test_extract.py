"""Parsing sample LLM outputs: fences, labels, clauses, verdicts, reasoning traces."""

from conftest import scenario

from nfv.extract import clauses, code_blocks, extract_code, extract_labeled, extract_verdict


def test_fenced_block_with_prose():
    text = scenario("shifted_mean")["dafny.theory"][0]
    code = extract_code(text)
    assert code.startswith("// doc: sum(iterable")
    assert "function Sum(l: seq<real>): real" in code
    assert "The program only calls" not in code


def test_last_matching_block_wins_and_language_filter():
    text = "```python\nx = 1\n```\nfirst:\n```dafny\nA\n```\nbetter:\n```Dafny\nB\n```\n"
    assert extract_code(text) == "B\n"
    assert extract_code(text, ("python",)) == "x = 1\n"


def test_untagged_fence_and_no_fence_fallbacks():
    assert extract_code("here:\n```\nmethod m() {}\n```") == "method m() {}\n"
    assert extract_code("method m() {}") == "method m() {}\n"
    assert extract_code("```python\nonly python\n```") is None
    assert extract_code("") is None


def test_reasoning_models_think_blocks_are_ignored():
    text = "<think>maybe ```dafny\nWRONG\n``` hmm</think>\n```dafny\nRIGHT\n```"
    assert extract_code(text) == "RIGHT\n"
    # a truncated reasoning trace yields nothing rather than half-thoughts
    assert extract_code("<think>still thinking ```dafny\nX\n```") is None


def test_labeled_spec_blocks():
    text = scenario("truncate_ref")["dafny.spec"][0]
    blocks = extract_labeled(text, "dafny", ("helpers", "ensures"))
    assert blocks["helpers"].strip() == ""
    assert clauses(blocks["ensures"], "ensures") == ["0.0 <= return_value < 1.0"]


def test_clause_splitting():
    code = "// from the docstring\nrequires |l| > 0\n  && l[0] >= 0\nrequires k >= 0;\n"
    assert clauses(code, "requires") == ["|l| > 0 && l[0] >= 0", "k >= 0"]
    assert clauses("// none\n", "requires") == []
    assert clauses("none", "requires") == []
    assert clauses("x > 0", "requires") == ["x > 0"]  # keyword omitted


def test_verdicts():
    j = scenario("judge")["judge"]
    assert extract_verdict(j[0]) == "INCORRECT"
    assert extract_verdict(j[1]) == "CORRECT"     # markdown bold tolerated
    assert extract_verdict("I think it is fine.") is None
    assert extract_verdict("VERDICT: CORRECT\n...on reflection...\nVERDICT: INCORRECT") == "INCORRECT"


def test_tilde_fences_and_info_labels():
    blocks = code_blocks("~~~dafny helpers\nfunction F(): int { 1 }\n~~~\n")
    assert blocks[0].lang == "dafny" and blocks[0].label == "helpers"

# Partial full-corpus run: Claude Opus, 2026-10-09 15:35–16:00 EDT

`claude -p --model opus --effort medium` (isolated: --safe-mode, no tools, empty cwd), pipeline arm
(refute, then verify), 2 attempts × 4 rounds, 12 entries in parallel, hard stop at 16:00.
**119 of 206 entries finished** (59 references, 60 mutants; mostly the lower-numbered HumanEval tasks; entries in
flight at 16:00 were dropped, which biases the sample towards faster/easier entries). 1107 LLM calls,
$51.19 at list price, 0 LLM errors.

| task | population | issued | wrong | precision | recall | F1 | paper (gpt-5.6-sol, 5×10) |
|---|---|---|---|---|---|---|---|
| verification (P ⊧ S) | 59 references | 45 | 1 | 97.8% | 74.6% | 0.85 | 93.5% / 69.9% / 0.80 |
| refutation (P ⊭ S) | 60 mutants | 57 | 4 | 93.0% | 88.3% | 0.91 | 88.7% / 45.6% / 0.60 |
| pipeline | 119 | 102 | 5 | 95.1% | 81.5% | 0.88 | 92.2% / 57.3% / 0.71 |

(`nfv report` prints the verification row over the 55 references that reached the verify arm; the 4 references
refuted first never ran it, so recall over all 59 references is 44/59.)

Wrong verdicts (paper Table 5 style), each checked by executing the Python:
* HumanEval_11 reference refuted with `a = "", b = "0"`: real violation (`|rv| = |a| = |b|` impossible for
  unequal lengths) — the very entry the paper lists as "missing precondition |a| = |b|".
* HumanEval_13 reference refuted with `a = b = 0`: real (`a % 0` raises ZeroDivisionError in S).
* HumanEval_17 reference refuted with `" "`: real (returns `[None, None]`, S violated).
* HumanEval_25 reference refuted with `n = 1`: real (`eval('')` raises SyntaxError in S).
* HumanEval_20 mutant verified: spurious proof. The mutant returns `sorted(pair)`, a *list*, and S checks
  membership among *tuples*; Python's `[a, b] in [(a, b)]` is False, but the Dafny model used `seq<real>`
  for both, erasing the list/tuple distinction (an incorrect formalization).

So all 4 false refutations are genuine Python counterexamples on inputs the docstring does not exclude (the
paper's conservative scoring still counts them as wrong); the only truly unsound verdict is a type-modeling error.

**Harness bug found by this run (fixed afterwards, commit after b9c33e2):** the audit parser read a cardinality
`|s|` right before a body brace as a set comprehension, so declarations such as
`function PyIndex(...) requires ... < |s| { ... }` looked bodyless and were rejected as "unproven assumptions";
it also garbled some `ensures` clauses ("goal edited"). Re-parsing every recorded response: 478 bodyless
detections before the fix, 39 after. These rejections could only cost recall (audits never issue verdicts),
so the recall numbers above are a lower bound for this configuration.

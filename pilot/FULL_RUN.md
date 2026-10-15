# Full corpus: Claude Opus via `claude -p` (2026-10-09 and 2026-10-15)

Settings throughout: `--model opus --effort medium`, isolated (`--safe-mode`, no tools, empty cwd), pipeline arm
(refute, then verify), **2 attempts × 4 rounds** (the paper uses 5 × 10), 12 entries in parallel.

* 10-09: 119 entries before a 16:00 stop (`full-opus-0409`, first 181 records). The audit parser still had the
  `|s| {` bug, which caused spurious "bodyless"/"goal edited" rejections (abstentions only).
* 10-15 phase 1: the remaining 87 entries with the fixed parser (`full-opus-0409`, records 182+).
* 10-15 phase 2: 84 of the first 119 re-run with the fixed parser (`full-opus-rerun`). Stopped by the usage
  guard: no new entries after the 5-hour session reached 75%; 4 in-flight entries aborted at 78% (unrecorded).
  Session usage went from 9% to 78%; weekly usage from 4% to 10%.

`merged-best` = all 206 entries, using the fixed-parser result wherever one exists (171 entries) and the
10-09 result for the other 35. `merged-fixed` = only the 171 fixed-parser entries.

## Results (recall over all entries of the class)

| | precision | recall | F1 | issued / wrong |
|---|---|---|---|---|
| **pipeline, all 206 (`merged-best`)** | **95.8%** | **88.8%** | **0.92** | 191 / 8 |
| verification, 103 references | 95.7% | 85.4% (88/103) | 0.90 | 92 / 4 |
| refutation, 103 mutants | 96.0% | 92.2% (95/103) | 0.94 | 99 / 4 |
| pipeline, 171 fixed-parser entries | 95.2% | 91.8% | 0.93 | 165 / 8 |
| paper (gpt-5.6-sol, 5 × 10): pipeline | 92.2% | 57.3% | 0.71 | 128 / 10 |
| paper: verification / refutation | 93.5% / 88.7% | 69.9% / 45.6% | 0.80 / 0.60 | |

(`nfv report` prints the verification row over the references that reached the verify arm (99); the 4 references
refuted first never ran it. The recall above counts them as misses.)

Parser fix, same 84 entries: old parser 76 issued / 5 wrong, recall 84.5% → fixed parser 83 issued / 4 wrong,
recall 94.0%. These are two different samples from the model, so the difference is not only the fix.

## The 8 wrong verdicts (paper Table 5 style)

| entry | verdict | cause |
|---|---|---|
| HumanEval_11 reference | refuted (`a="", b="0"`) | real Python counterexample; the paper's own Table 5 entry (missing precondition \|a\|=\|b\|) |
| HumanEval_111 reference | refuted (`"a ab"`) | real; the paper's Table 5 entry (tokens are single letters) |
| HumanEval_17 reference | refuted (`" "`) | real (returns `[None, None]`), docstring does not exclude it |
| HumanEval_25 reference | refuted (`n = 1`) | real (`eval('')` raises in S) |
| HumanEval_89 mutant | verified | stronger precondition (lowercase letters only); the mutant fails on digits |
| HumanEval_20 mutant | verified | type erasure: returns a list, S compares against tuples; both modeled as `seq` |
| HumanEval_148 mutant | verified | type erasure: `isinstance(rv, tuple)` translated to `true`, the mutant returns a list |
| HumanEval_128 mutant | verified | type erasure: `isinstance(rv, int)` vs a float result; int/float both `real` |

All 4 false refutations are genuine Python counterexamples on inputs the docstring does not exclude (the
paper scores them as wrong, and so do we). Of the 4 unsound proofs, 3 come from erasing Python's dynamic
types. The spec prompt's advice to translate `isinstance` checks to `true` contributes, and that is the next
thing to fix (e.g. distinct Dafny types for tuple vs list and int vs float, or a type-tagged value model).

## Caveats against the paper

A different model, our own prompts, a smaller budget (2 × 4 vs 5 × 10), the witness `NONE` option (abstention
only), a merged run (35 entries from the buggy-parser day), and a stochastic model sampled once per entry.
The precision is comparable to the paper's (~96% vs 92%); the recall is much higher (89% vs 57%).

Cost at list price (subscription usage, not billed): 2334 LLM calls, about $106 in total for both days.

# Full corpus: Claude Opus via `claude -p`

## Final numbers (2026-10-15): all 206 entries with the fixed audit parser

Every entry has a fixed-parser result: 87 from 10-15 phase 1, 84 from phase 2, and 35 from phase 3
(19:12–19:22, session usage 4% → 17%, no aborts). Records: `final-fixed/results.jsonl`.
Settings: `--model opus --effort medium`, isolated, pipeline arm, 2 attempts × 4 rounds, 12 in parallel.

Scored as in the paper (Table 3): precision counts every verdict of that kind across all 206 entries
(e.g. a verified mutant is a wrong "verified"); recall is over the class's 103 entries.

| task | population | issued | wrong | precision | recall | F1 | paper (gpt-5.6-sol, 5 × 10) |
|---|---|---|---|---|---|---|---|
| verification (P ⊧ S) | 103 | 98 | 5 | 94.9% | 90.3% | 0.93 | 93.5% / 69.9% / 0.80 |
| refutation (P ⊭ S) | 103 | 100 | 4 | 96.0% | 93.2% | 0.95 | 88.7% / 45.6% / 0.60 |
| **pipeline (refute, then verify)** | **206** | **198** | **9** | **95.5%** | **91.7%** | **0.94** | 92.2% / 57.3% / 0.71 |

By library tier (paper Table 4 layout):

| tier | proved | bad | prec | refuted | bad | prec |
|---|---|---|---|---|---|---|
| L0 | 21/22 (95%) | 0 | 100% | 21/22 (95%) | 0 | 100% |
| L1 | 17/18 (94%) | 1 | 94% | 17/18 (94%) | 0 | 100% |
| L2a | 31/35 (89%) | 2 | 94% | 32/35 (91%) | 2 | 94% |
| L2b | 21/23 (91%) | 2 | 91% | 21/23 (91%) | 1 | 95% |
| L3 | 3/5 (60%) | 0 | 100% | 5/5 (100%) | 1 | 83% |

The 9 wrong verdicts:

| entry | verdict | cause |
|---|---|---|
| HumanEval_11 reference | refuted (`a="", b="0"`) | real Python counterexample; the paper's own Table 5 entry (\|a\|=\|b\| unstated) |
| HumanEval_111 reference | refuted (`"a ab"`) | real; the paper's Table 5 entry (tokens are single letters) |
| HumanEval_17 reference | refuted (`" "`) | real (returns `[None, None]`) |
| HumanEval_25 reference | refuted (`n = 1`) | real (`eval('')` raises in S) |
| HumanEval_89 mutant | verified | stronger precondition (lowercase letters only); the mutant fails on digits |
| HumanEval_20 mutant | verified | type erasure: list vs tuple, both `seq` |
| HumanEval_68 mutant | verified | type erasure: returns a tuple, S requires `isinstance(rv, list)`; both `seq<int>` |
| HumanEval_148 mutant | verified | type erasure: `isinstance(rv, tuple)` → `true`, the mutant returns a list |
| HumanEval_128 mutant | verified | type erasure: `isinstance(rv, int)` vs a float result; int/float both `real` |

All 4 false refutations are genuine Python counterexamples on inputs the docstring does not exclude. Of the
5 unsound proofs, 4 come from erasing Python's dynamic types (list/tuple, int/float). This is the clearest
remaining weakness, and the spec prompt's advice to translate `isinstance` checks to `true` contributes.

---

## History (earlier, superseded numbers)


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

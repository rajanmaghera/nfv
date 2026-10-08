# Pilot: Claude Opus via `claude -p` on three easy tasks (2026-10-08)

**Setup.** HumanEval/53 `add` (L0), HumanEval/2 `truncate_number` (L1) and HumanEval/42 `incr_list` (L0):
6 entries, a reference and a mutant for each. Claude Code 2.1.294 ran
`claude -p --model opus --effort medium`, isolated with `--safe-mode --strict-mcp-config --setting-sources ""
--tools "" --disallowedTools "mcp__*" --no-session-persistence`, from a fresh, empty temporary working directory.
(`--bare` cannot be used with an OAuth login: it skips OAuth and needs `ANTHROPIC_API_KEY`.) The budget was
2 attempts × 4 proof rounds, smaller than the paper's 5 × 10. Dafny 4.11 and CBMC 6.9 ran with `--jobs 3`.

Each directory holds `results.jsonl`, `transcript.jsonl` (every prompt and response, replayable with
`--backend replay:pilot/<run>/transcript.jsonl`) and `config.json`. Run `python3 -m nfv report pilot/<run>` to
print the tables.

## Results

| run | arm | verdicts issued | wrong | precision | recall | LLM calls | cost (USD) |
|---|---|---|---|---|---|---|---|
| `pilot-opus-1` | pipeline (refute → verify) | 6 / 6 | 0 | 100% | 100% | 72 | 1.64 |
| `pilot-opus-2` (witness may answer NONE) | pipeline (refute → verify) | 6 / 6 | 0 | 100% | 100% | 32 | 0.59 |
| `pilot-opus-cbmc` | CBMC refutation | 4 | **1** | 75% | 100% | 28 | 0.72 |
| `pilot-opus-cbmc-2` (with `NFV_MODEL_DOMAIN`) | CBMC refutation | 3 | 0 | 100% | 100% | 29 | 0.70 |
| `pilot-opus-judge` | LLM-as-judge | 6 / 6 | 0 | 100% | 100% | 6 | 0.09 |

Every Dafny verdict carries a machine-checked artifact (`results.jsonl` → `artifact`). As expected for easy
tasks, the judge is also perfect here. The paper's gap (72% vs 92% precision) shows up only on the full corpus.

## Findings and the changes they led to

1. **Isolation.** The model never saw the repository or the user's setup. Asked directly, it reported no
   CLAUDE.md, memory or project instructions. Because tools are disabled, every verifier run is the harness's.

2. **The forced witness wasted most of the budget** (`pilot-opus-1`). Asked for a counterexample to each
   correct program, Opus said every time that none exists ("No input satisfying `number > 0.0` violates the
   postcondition… I deliberately did not use `requires false`"). The prompt still required an input, so it
   supplied a placeholder. Every round of the doomed refutation proof then ran, and attempt 2 rebuilt the
   whole front end only to fail again. That was 24 proof calls plus 4 front-end calls per entry pair: 55 of
   the 72 calls went to the three correct programs.
   **Change:** the witness stage may now answer `// NONE`. The refutation arm then abstains at once
   (failure kind `no-witness`). This does not affect soundness: it only adds abstentions, never verdicts.
   Result in `pilot-opus-2`: the same 6/6 at 32 calls and $0.59, down from 72 calls and $1.64 (−64% cost).

3. **CBMC produced a spurious refutation** of the *correct* HumanEval/2 reference (`pilot-opus-cbmc`).
   Opus's C model of `int(float)` saturated to `LLONG_MAX` outside the `long long` range. CBMC then found
   `number = 2.68e154`, where C computes `number − 9.2e18 ≈ 2.68e154`, while Python's exact big-int
   `int()` gives `0.0`. This is the failure mode the paper describes for bounded verifiers (§3.3): an
   under-constrained or unfaithful environment model E_F. The model's own comment said the result was only
   valid inside the range, but nothing enforced that.
   **Change:** library and translation code can call `NFV_MODEL_DOMAIN(cond)`, which the harness expands to
   `__CPROVER_assume(cond)`. The C prompt forbids saturated or made-up values outside the faithful domain.
   For bounded *refutation* an assumption only prunes executions, so every counterexample that survives is
   genuine. In `pilot-opus-cbmc-2`, Opus guarded `int()` with
   `NFV_MODEL_DOMAIN(x >= -9223372036854775808.0 && x < 9223372036854775808.0)`, and the false refutation
   disappeared. Two smoke tests pin both behaviours down.

4. **Mutable arguments and the post-state** (HumanEval/42's mutant mutates `l` in place). Opus translated
   this with `var l := l;`, so `l` in S_F means the *pre-call* list. The upstream nl2postcond harness
   evaluates S_L *after* the call on the same objects, so there `l` is the *post-call* list. Both readings
   refute this mutant. Across the corpus, 5 entries mutate a parameter that their postcondition mentions
   (20, 42m, 47m, 70m). Only 42m depends on the reading; the others sort in place under
   permutation-invariant postconditions. **Change:** dataset validation now uses the upstream post-state
   semantics. All 103 pairs still validate and no labels changed.

5. **The staged discipline held up.** No proof-search response in any run was rejected by the freeze or
   no-assumption audits. Opus only added invariants, asserts and ghost snapshots, and it volunteered that
   `requires false` would be a vacuous cheat. Reachability checks passed for every refutation.
   Opus-produced artifacts look like the paper's Figure 2. For example, its theory for `int()` quotes the
   docstring and defines truncation through `Floor`, and its precondition `number > 0.0` comes from "Given a
   positive floating point number".

6. **Cost profile.** Most calls take 4–13 s. Proof-repair calls are the most expensive (about $0.035 each,
   because they resend the whole artifact). At `pilot-opus-2` rates, the full 206-entry corpus would cost
   very roughly $20–60 with Opus at medium effort. Harder tiers will need more rounds, so expect the high
   end or more.

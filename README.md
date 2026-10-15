# NFV replication: neuro-formal verification of Python with Dafny and CBMC

A re-implementation of the evaluation in **"Neuro-Formal Verification: Agentic Language-Agnostic
Formal Program Reasoning"** (S. K. Lahiri, arXiv:2608.21516). The paper's code has not been released;
everything here is rebuilt from the paper's text.

The task: given a Python program `P`, a Python postcondition `S_L` (an `assert` over the
parameters and `return_value`) and the docstring, decide whether `P` satisfies `S_L`, and back
every verdict with a machine-checked artifact. Any LLM can drive the pipeline: a local model
behind an OpenAI-compatible server (llama.cpp, Ollama, vLLM), or `claude -p`.

## What is implemented

| Paper | Here |
|---|---|
| §2, App. A.1: **nl2post-safe**, 206 entries (103 HumanEval references + 103 mutants) | `scripts/build_dataset.py` → `data/nl2post_safe.jsonl` |
| Table 1: library tiers L0–L3 | `nfv/tiers.py` (approximate, see below) |
| §3.1.1: staged, goal-blind **verification** arm (1a theory → 2 tagged translation → 1b precondition → 3 spec → 4 proof search) | `nfv/dafny_pipeline.py::verification_arm` |
| Provenance tags, freeze checks, "no unproven assumptions", precondition consistency | `nfv/dafny_artifact.py` |
| §3.1.2: **refutation** arm (negated goal, witness as precondition, reachability + goal-shape checks) | `nfv/dafny_pipeline.py::refutation_arm` |
| Table 3 "pipeline": refute first, verify what is left | `pipeline()` (shares stages 1a/2/1b/3 per attempt) and `metrics.compose_pipeline` |
| §3.2, §4.3: **CBMC** bounded refutation arm with UB checks | `nfv/cbmc_pipeline.py` |
| §4.5: LLM-as-judge and unstaged agent–verifier baselines | `nfv/baselines.py` |
| §4.1: 5 attempts × 10 repair rounds; precision / recall / F1 | `Budget`, `nfv/metrics.py`, `nfv report` |
| Tables 3, 4, 5, 6 layouts | `nfv report RUN_DIR...` |

Not reproduced: the Nagini baseline, and stage 5 (mapping proofs back to Python), which the paper
does not evaluate either.

## Quick start

```bash
nix develop                       # python3 + pytest, dafny 4.11, cbmc 6.9
python3 -m nfv check              # verifier versions + dataset summary
python3 -m pytest -q tests        # offline smoke tests (~1 min; no LLM, no GPU)
```

Everything runs inside `nix develop`. `nix run . -- <command>` also works, and
`nix flake check` runs the smoke tests in the Nix sandbox.

### Running with `claude -p`

```bash
NFV_CLAUDE_BIN=~/.local/bin/claude python3 -m nfv run --arm pipeline --backend claude --model opus \
    --effort medium --jobs 3 --out runs/opus
python3 -m nfv report runs/opus
```

Each stage is one stateless call, with the prompt on stdin. Claude is isolated from this repository and from
your setup:

* every call runs in a fresh, empty temporary working directory;
* tools are off (`--tools "" --disallowedTools "mcp__*"`), so Claude answers from the prompt like any other
  backend and the harness runs the verifier;
* `--claude-isolation safe-mode` (the default) adds `--safe-mode --strict-mcp-config --setting-sources ""`.
  This turns off CLAUDE.md, memory, skills, plugins, hooks and MCP while normal login keeps working.
  `--claude-isolation bare` uses `--bare` instead, which also skips OAuth and so needs `ANTHROPIC_API_KEY`;
* `--no-session-persistence --output-format json`, plus `--model`, `--effort` and `--system-prompt`.

Override the flags entirely with `--claude-args "..."` or `NFV_CLAUDE_ARGS`. Pilot results with Opus are in
[`pilot/README.md`](pilot/README.md).

### Running with a local LLM

```bash
nix develop .#local-llm                                   # adds llama.cpp
llama-server -m /models/qwen2.5-coder-32b-q5.gguf -c 32768 --port 8080 &
python3 -m nfv run --arm verify --backend local --base-url http://localhost:8080/v1 --model qwen --out runs/qwen
# or Ollama:  --backend ollama --model qwen2.5-coder:32b   (http://localhost:11434/v1)
```

Any OpenAI-compatible `/chat/completions` endpoint works. `<think>…</think>` reasoning is stripped
before parsing.

### Useful options

```
--arm verify|refute|pipeline|cbmc|judge|unstaged
--tasks 2,30,53   --ids HumanEval_2/mutant   --kind reference|mutant   --limit N
--attempts 5 --rounds 10 --stage-rounds 3      # the paper's budget is 5 x 10
--jobs N                                        # entries in parallel
--dafny-time-limit 30 --dafny-cores 2 --cbmc-unwind 8 --temperature 0.2
python3 -m nfv prompt theory HumanEval_133/reference   # inspect a stage prompt, no LLM call
python3 -m nfv show HumanEval_2/mutant
```

`--trace` additionally writes `trace.jsonl` with every LLM call, verifier run and pipeline event, in order
(see [`demo/`](demo/README.md) for an animated walkthrough built from one). A run directory holds `config.json`, `results.jsonl` (one record per entry and arm: verdict,
failures per attempt, witness, final artifact, event log), `transcript.jsonl` (every prompt and
response; `--backend replay:<file>` replays it) and `artifacts/<entry>/<arm>/attempt_k/*.dfy|*.c`.
Runs can be resumed: entries already in `results.jsonl` are skipped (pass `--fresh` to redo them).

## How the staged discipline is enforced

Each stage sees only what the paper allows (`nfv/prompts.py`):

| stage | sees | never sees |
|---|---|---|
| 1a library theory | Python source, runtime docstrings of the built-ins it calls (`sum.__doc__`, …) | `S_L` |
| 2 translation | Python source (numbered), frozen theory | `S_L` |
| 1b precondition | docstring, signatures, theory | `S_L`, translation body |
| 3 specification | `S_L`, docstring, signatures, theory | translation body |
| 4 proof search | assembled artifact + verifier output | — (may only add ghost code) |
| refutation witness | Python source + `S_L` | Dafny translation body |

The repair loops for stages 1a, 2, 1b and 3 get only `dafny resolve` errors and audit messages,
never a verifier goal. Stage 1b runs after stage 2 so the precondition can use the Dafny parameter
types. It still never sees the specification or the method body.

The harness assembles the artifact itself (`THEORY` / `SPEC` / `PROGRAM` regions) and inserts the
frozen `requires` / `ensures`. Each proof-repair round is then checked mechanically:

* **Provenance:** every code line sits under a `// src: source.py:N | <verbatim Python>` or
  `// synth: <kind>` tag. Untagged code is rejected, and every quote is checked against the source.
* **Freeze:** the theory and spec regions must be byte-identical, and the entry method's contract
  and every `src:` line must be unchanged. The **ghost erasure** of the program region (with
  asserts, invariants, decreases, calc, ghost vars, lemma calls and lemmas removed) must match the
  stage-2 translation token for token. Proof search can add proof but cannot change the program.
* **No unproven assumptions** outside the theory: no `assume`, `expect`, `{:axiom}`, bodiless lemmas
  or functions, `{:verify false}`, `{:only}`, `decreases *`, `include` or `extern`. Attributes go
  through an allow-list. Dafny's own axiom warnings are also treated as failures.
* **Spec independence:** the precondition, spec and witness may not mention anything the
  translation declares.
* **Precondition consistency:** `lemma (params) requires PRE ensures false {}` must *not* verify.
* **Refutation:** the goal must be exactly `!(S_F)`. In the reachability check, the proved artifact
  with `ensures false` must *not* verify. The witness stage may answer `// NONE` when it believes the program
  is correct. The arm then abstains without spending proof rounds. This is a deviation from the paper,
  added after the Opus pilot (see `pilot/README.md`); it can only add abstentions, never verdicts.

The CBMC arm uses the same tags. Its harness must contain exactly one
`__CPROVER_assume(nfv_pre(..))`, one call to the function under test and one
`__CPROVER_assert(nfv_spec(..), "nfv_spec")`, and the translated code must be deterministic.
CBMC runs with bounds, pointer, overflow, conversion, division and shift checks. Where C cannot model
Python faithfully (unbounded ints, `int()` of a huge float), the code must say so with
`NFV_MODEL_DOMAIN(cond)`, which expands to `__CPROVER_assume(cond)`. Returning saturated or made-up values
is not allowed. For refutation an assumption only prunes executions, so every counterexample that
survives is genuine. When one of
those undefined-behaviour checks fails, the stage that owns the failing function gets a
goal-blind repair prompt. Only a trace violating `nfv_spec` with no undefined behaviour counts
as a refutation.

## Dataset

`python3 scripts/build_dataset.py --validate` rebuilds `data/nl2post_safe.jsonl` from the
upstream [nl2postcond](https://github.com/microsoft/intent-formalization/tree/main/nl2postcondition-fse2024)
artifacts. These are `gpt4_simple_no_ref.zip` (postconditions and the `<entry>_original`
reference implementations) and `distinct_code_mutants.jsonl.zip` (mutants and killing inputs).
The build pins entries by the paper's Table 7 (`data/paper_table7_pins.json`, extracted from the
paper) and sanitizes each postcondition: comments, assertion messages and unused imports are
dropped, then the result goes through `ast.unparse`.

* All 103 sanitized postconditions match Table 7 exactly.
* With `--validate`, every reference satisfies its postcondition on all of its mutant's killing
  inputs, and every mutant violates it on at least one. Each check runs as a subprocess. As in the
  upstream harness, S_L is evaluated after the call on the same argument objects (post-state). This
  matters for parameters mutated in place: of the 5 such entries, only HumanEval/42's mutant depends on it.

**Known deviation:** the paper does not publish the name lists behind its tiers. Our classifier
gets the paper's Appendix B examples right, but gives 22/18/35/23/5 tasks for L0/L1/L2a/L2b/L3,
against the paper's 21/22/25/26/9.

Licenses: nl2postcond is MIT; its EvalPlus-derived code is Apache-2.0; HumanEval is MIT.

## Tests

`tests/` holds offline smoke tests. Model output comes from sample LLM responses in
`tests/fixtures/llm/<scenario>/<stage>.<n>.md`, written the way a chat model answers: prose,
fenced blocks, and some deliberately wrong answers. Dafny and CBMC themselves run for real.
No test calls a real LLM or the real `claude` binary:

* `test_llm_backends.py`: the `claude -p` backend is driven through a fake `claude` executable
  (`tests/fake_claude.py`). The test checks the flags, that the prompt goes on stdin, JSON and
  stream-json parsing, and errors. The OpenAI-compatible backend is tested against a localhost
  HTTP stub, including retry and `<think>` stripping. Transcript and replay are covered too.
* `test_artifact_audit.py`: provenance, forbidden constructs, the freeze and ghost erasure, each
  tested against cheating attempts (rewritten code, weakened goal, stronger precondition, edited
  theory, `assume`, axiom lemmas, dropped `src:` lines).
* `test_dafny_pipeline.py`: the paper's running example, which needs repairs in stages 2 and 1b,
  has a cheating proof rejected, then verifies with the paper's two lemmas. Its buggy variant is
  refuted with `l == [0, 0]`. Dataset task HumanEval/2 goes through the combined pipeline.
  Also covered: a proof search that only cheats ends in abstention, a vacuous witness is caught
  by the reachability check, and the spec may not reference the translation.
* `test_cbmc_pipeline.py`: the mutant of HumanEval/2 is refuted after a goal-blind repair of an
  undefined-behaviour failure, and the reference gets no counterexample.
* `test_metrics.py`: reproduces the arithmetic of the paper's Table 3 and of its judge baseline.
* `test_baselines.py`, `test_cli_e2e.py`: the judge, the unstaged baseline (which "proves" a buggy
  program by rewriting it), and full `nfv run` / `nfv report` runs through the fake `claude -p`.

## Layout

```
flake.nix / flake.lock      nix dev shells (default, local-llm), app, checks
data/                       nl2post_safe.jsonl (206 entries), Table 7 pins
scripts/build_dataset.py    dataset reconstruction + validation
nfv/llm.py                  backends: claude -p, OpenAI-compatible, scripted, replay, transcript
nfv/prompts.py              per-stage prompts
nfv/dafny_artifact.py       artifact layout, tokenizer, audits, ghost erasure
nfv/dafny_pipeline.py       verification / refutation arms, attempts x rounds, pipeline
nfv/cbmc_pipeline.py        bounded refutation arm
nfv/baselines.py            LLM-as-judge, unstaged agent-verifier
nfv/metrics.py              precision / recall / F1, paper-style tables
nfv/tools.py                dafny / cbmc runners and output parsing
nfv/libdocs.py              E_nl: runtime docstrings of the library surface
nfv/cli.py                  `python -m nfv ...`
tests/                      smoke tests + sample LLM outputs
pilot/                      Claude Opus pilot runs (results, transcripts) and findings
demo/                       one entry traced end to end: animated GIF, PDF deck, full call dump
scripts/{dump_trace,make_demo,render_demo}.py   trace -> markdown dump / slide deck / GIF + PDF
scripts/run_stats.py        per-entry calls / cost / events of a run
```

"""Prompt templates, one per stage.

Each prompt shows a stage only what the paper's staged, goal-blind discipline
allows it to see:

  stage            sees                                             never sees
  1a theory        Python source, library docstrings (E_nl)         S_L, docstring precondition
  2  translate     Python source, frozen theory                     S_L
  1b precondition  docstring, signatures, frozen theory             S_L, translation body
  3  spec          S_L, docstring, signatures, theory               translation body
  4  proof         the assembled artifact + verifier output         (may change ghost code only)
  witness          Python source + S_L                              Dafny translation body

Every prompt begins with ``[NFV-STAGE: <name>]``.
"""

from __future__ import annotations

from .dataset import Entry, numbered

SYSTEM = (
    "You are an expert in formal verification, Dafny and C, working as one stage of an automated "
    "verification pipeline. Your answer is parsed by a program: reply with exactly the fenced code "
    "block(s) requested, plus at most a few sentences of explanation. Do not ask questions; you have no tools."
)

DAFNY_CONVENTIONS = """\
Python -> Dafny modeling conventions (shared by every stage; follow them exactly):
- Python int -> Dafny `int` (unbounded, like Python). bool -> `bool`.
- Python float -> Dafny `real` (exact arithmetic: this is a modeling assumption; no NaN/inf/rounding).
- Python str -> `string` (= seq<char>); a one-character string used as a character -> `char` only if the
  Python code never treats it as a string.
- list[T] / tuple of unknown length -> `seq<T>`; fixed-size tuple -> Dafny tuple `(A, B)`; set -> `set<T>`;
  dict -> `map<K, V>` (note: insertion order is lost); None / Optional[T] -> `Option<T>` declared in the theory as
  `datatype Option<T> = None | Some(value: T)` when needed.
- Python `//` and `%` floor towards -infinity; Dafny's `/` and `%` on int are Euclidean. They agree only when the
  divisor is positive. Use theory functions (e.g. PyFloorDiv, PyMod) wherever the sign may matter.
- Negative indices and slices with defaults/negatives need theory helpers; Dafny `s[a..b]` requires 0<=a<=b<=|s|.
- If a parameter has no type annotation, choose the type the docstring implies and keep it consistent.
- The function under test becomes `method <entry_point>(<params>) returns (return_value: <T>)`.
- Helper Python functions become Dafny functions or methods with the same names.
"""

TAG_RULES = """\
Provenance tags (mandatory; untagged code is rejected mechanically):
- Every line of Dafny code must sit under a tag comment. A tag applies to the code lines that follow it, up to
  the next tag.
- `// src: source.py:N | <Python line N, quoted verbatim>` marks code translated from Python line N
  (use `source.py:N-M | <lines N..M joined by single spaces>` for a statement spanning several lines).
- `// synth: <kind>, <free text>` marks code with no source counterpart, e.g. `// synth: declaration`,
  `// synth: translation helper, for the generator expression`, `// synth: invariant`, `// synth: decreases`.
- Quotes are checked character-for-character against the numbered source (leading/trailing spaces ignored).
"""


def _stage(name: str) -> str:
    return f"[NFV-STAGE: {name}]\n"


def _python(entry: Entry) -> str:
    return f"```python\n# source.py\n{numbered(entry.source)}\n```"


def repair_suffix(previous: str, feedback: str, lang: str = "dafny") -> str:
    return (f"\n\n## Your previous answer\n```{lang}\n{previous.rstrip()}\n```\n\n"
            f"## Checker feedback\n```\n{feedback.strip()[:6000]}\n```\n\n"
            "Fix the problems and answer again, in the same format as requested above.")


# ---------------------------------------------------------------- stage 1a


def theory(entry: Entry, docs: str) -> str:
    return _stage("dafny.theory") + f"""\
Stage 1a: formalize the Python LIBRARY SURFACE that the program below uses, as a Dafny library theory.

You are NOT translating the program and you are NOT told what property will be checked. Write only Dafny
definitions that describe what the Python built-ins / methods / modules called by the program mean, as their
documentation states. Each definition must be checkable against the documentation by a reader.

{DAFNY_CONVENTIONS}
Rules:
- Prefer total recursive `function`/`predicate` definitions with bodies (they are proved, not assumed).
- Only if a function cannot reasonably be defined, declare it bodiless with `{{:axiom}}` and give its documented
  behaviour as `ensures` clauses, e.g. `function {{:axiom}} PyCeil(x: real): int ensures PyCeil(x) as real >= x ...`.
  Every axiom is an assumption a human must review, so keep them few and faithful to the docs.
- Do NOT model the program's own functions here, and do not write methods. No `assume`, no `include`.
- Name each definition after the Python entity it models (e.g. `Sum`, `PySorted`, `StrLower`).
- Precede each definition with a comment quoting the documentation line it formalizes.
- If the program uses no library functions beyond arithmetic, indexing, len and range, answer with an empty block.

## Program (for reference only: which library calls it makes, and with what argument types)
{_python(entry)}

## Library documentation (E_nl: Python runtime docstrings)
{docs or "(no library calls beyond the core language)"}

Answer with ONE fenced block:
```dafny
// your library theory
```"""


# ---------------------------------------------------------------- stage 2


def translate(entry: Entry, theory_code: str) -> str:
    return _stage("dafny.translate") + f"""\
Stage 2: translate the Python program into Dafny, line by line, as faithfully as possible.

You are not told what property will be checked: make the Dafny program behave exactly like the Python one,
including on unusual inputs. Do not add preconditions, postconditions, invariants or proofs - those come later.

{DAFNY_CONVENTIONS}
{TAG_RULES}
Further rules:
- The entry method must be `method {entry.entry_point}(...) returns (return_value: T)` with NO requires/ensures,
  and its opening `{{` must be on a line of its own.
- You may use (but not redefine) everything in the frozen library theory below; it is prepended to your code.
- Loops: translate `for x in xs` to an index-based `while` loop (no invariants yet).
- No `assume`, `expect`, `{{:axiom}}`, `{{:extern}}`, `{{:verify false}}`, `decreases *`.

## Python program (line numbers are for src tags)
{_python(entry)}

## Frozen library theory (already in scope)
```dafny
{theory_code.strip() or "// (empty)"}
```

Answer with ONE fenced block containing the translation:
```dafny
// src: source.py:N | ...
method {entry.entry_point}(...) returns (return_value: ...)
{{
  ...
}}
```"""


# ---------------------------------------------------------------- stage 1b


def precondition(entry: Entry, theory_code: str, header: str) -> str:
    return _stage("dafny.precondition") + f"""\
Stage 1b: state the PRECONDITION of the function, from its documentation only.

Write Dafny `requires` clauses over the parameters of the Dafny method below that capture the input assumptions
the docstring states (explicitly or by its examples' domain), e.g. "l must be non-empty" -> `requires |l| > 0`.
Do not invent assumptions the documentation does not support, and do not try to make any particular property
provable: you are not told the property, and the precondition will be frozen before it is revealed.

## Python signature and docstring
```python
{entry.signature()}
    \"\"\"{entry.docstring}\"\"\"
```

## Dafny method signature (the translation)
```dafny
{header}
```

## Frozen library theory (in scope)
```dafny
{theory_code.strip() or "// (empty)"}
```

Answer with ONE fenced block containing zero or more `requires` lines (write `// none` if there is no assumption):
```dafny
requires ...
```"""


# ---------------------------------------------------------------- stage 3


def spec(entry: Entry, theory_code: str, header: str) -> str:
    return _stage("dafny.spec") + f"""\
Stage 3: translate the Python postcondition S_L into a Dafny postcondition S_F, faithfully.

S_L is a Python assertion over the parameters and `return_value` (the function's result). Render exactly what it
says - no stronger, no weaker - under the shared modeling conventions. Use the frozen library theory for Python
built-ins it mentions; if S_L needs a built-in the theory lacks, define it as a spec helper (a function with a
body, faithful to Python's documented semantics; never a copy of the program's code). Python type checks such as
`isinstance(return_value, int)` are discharged by Dafny's types: translate them to `true`.

{DAFNY_CONVENTIONS}
## Python postcondition S_L
```python
{entry.postcondition}
```

## Python signature and docstring
```python
{entry.signature()}
    \"\"\"{entry.docstring}\"\"\"
```

## Dafny method signature (the translation)
```dafny
{header}
```

## Frozen library theory (in scope)
```dafny
{theory_code.strip() or "// (empty)"}
```

Answer with up to TWO fenced blocks. Optional spec helpers (functions/predicates with bodies only):
```dafny helpers
// optional
```
and the postcondition as one or more `ensures` clauses (their conjunction is S_F):
```dafny ensures
ensures ...
```"""


# ---------------------------------------------------------------- stage 4


def proof(entry: Entry, artifact: str, feedback: str, goal: str = "verification") -> str:
    what = ("prove that the method satisfies its postcondition" if goal == "verification" else
            "prove the NEGATED postcondition, i.e. that on the witness input the method violates S_F")
    return _stage("dafny.proof" if goal == "verification" else "dafny.refute.proof") + f"""\
Stage 4: proof search. The Dafny file below must verify; your job is to {what}.

You may ONLY add or change ghost proof code, each piece under a `// synth: proof` (or `// synth: invariant`,
`// synth: decreases`, `// synth: lemma`) tag:
- loop `invariant`s and `decreases` clauses, `assert` statements (incl. `assert ... by {{ ... }}`), `calc` blocks,
  `ghost var`s, calls to lemmas, and new `lemma`s / `ghost function`s / `ghost predicate`s with bodies
  (place new top-level lemmas inside the PROGRAM region, e.g. before the method).
Everything else is frozen and re-checked mechanically on every round: the THEORY and SPEC regions, every
`// src:` line and the executable code under it, the method signature, and its `requires`/`ensures`.
Forbidden: `assume`, `expect`, `{{:axiom}}`, bodiless lemmas, `{{:verify false}}`, `decreases *`, editing the
region marker lines. If the goal is false, you will not be able to prove it - do not try to cheat.

## Python source (for understanding)
{_python(entry)}

## Current artifact
```dafny
{artifact.rstrip()}
```

## Verifier / checker output
```
{feedback.strip()[:6000]}
```

Answer with the COMPLETE updated file (all three regions, markers unchanged) in ONE fenced block:
```dafny
...
```"""


# ---------------------------------------------------------------- refutation witness


def witness(entry: Entry, header: str, pre_clauses: list[str]) -> str:
    pre = "\n".join(f"requires {c}" for c in pre_clauses) or "// (none)"
    return _stage("dafny.witness") + f"""\
Refutation: find an input on which the Python function VIOLATES its postcondition S_L, while satisfying its
documented precondition.

Think about the program's behaviour on edge cases (empty inputs, zero, negatives, duplicates, boundaries...).
Express the input as Dafny `requires` clauses pinning each parameter of the Dafny method to a concrete value
(e.g. `requires l == [0.0, 0.0]`), using the shared modeling conventions. It will be installed as an extra
precondition and the negated postcondition will then have to be PROVED, so pick an input on which the violation
is certain.

## Python program
{_python(entry)}

## Python postcondition S_L
```python
{entry.postcondition}
```

## Dafny method signature
```dafny
{header}
```

## Frozen precondition (your input must satisfy it)
```dafny
{pre}
```

{DAFNY_CONVENTIONS}
Answer with ONE fenced block of `requires` clauses:
```dafny
requires ...
```
If you are convinced that NO allowed input violates S_L, do not invent a placeholder input: answer with a block
containing only `// NONE`. The refutation then abstains, which is the right outcome for a correct program.
Never use `requires false` or any other contradictory input."""


# ---------------------------------------------------------------- baselines


def judge(entry: Entry) -> str:
    return _stage("judge") + f"""\
Does the Python function below satisfy the postcondition for every input allowed by its docstring?

{_python(entry)}

Postcondition (a Python assertion over the parameters and `return_value`, the function's result):
```python
{entry.postcondition}
```

Input assumptions stated by the docstring:
\"\"\"{entry.docstring}\"\"\"

Reason briefly, then finish with exactly one line: `VERDICT: CORRECT` (the function always satisfies the
postcondition on allowed inputs) or `VERDICT: INCORRECT` (some allowed input violates it)."""


def unstaged(entry: Entry) -> str:
    return _stage("unstaged") + f"""\
Write a Dafny program containing a method `{entry.entry_point}` that implements the Python function below and
carries the given postcondition (with `return_value` as the result), such that `dafny verify` succeeds.

{_python(entry)}

Postcondition:
```python
{entry.postcondition}
```

Answer with ONE fenced block:
```dafny
...
```"""


# ---------------------------------------------------------------- CBMC arm

C_CONVENTIONS = """\
Python -> C modeling conventions (shared by every stage; follow them exactly):
- Include only <stdbool.h>, <stddef.h>, <stdint.h>, <string.h>, <limits.h>, <math.h>.
- Python int -> `long long`; bool -> `bool`; float -> `double`.
- Python list of T -> `typedef struct { T *data; long long len; } list_<t>;` (e.g. `list_ll`, `list_double`);
  str -> `typedef struct { char *data; long long len; } str_t;` (not NUL-terminated); declare these in the library
  stage. Results are written to fresh fixed-size buffers or static storage; no malloc needed.
- Bounds: every collection the harness builds has at most NFV_MAX_LEN (= 5) elements; loops are bounded.
- The C program must be free of undefined behaviour (CBMC checks bounds, pointers, overflow, conversions, division).
"""


def c_theory(entry: Entry, docs: str) -> str:
    return _stage("c.theory") + f"""\
Stage 1a (C backend): implement, in C, the Python library functions the program below calls, from their
documentation, plus the shared type declarations. Do not translate the program itself; you are not told what
property will be checked.

{C_CONVENTIONS}
## Program (for reference: which library calls it makes)
{_python(entry)}

## Library documentation
{docs or "(no library calls beyond the core language)"}

Answer with ONE fenced block:
```c
#define NFV_MAX_LEN 5
// types and library functions
```"""


def c_translate(entry: Entry, theory_code: str) -> str:
    return _stage("c.translate") + f"""\
Stage 2 (C backend): translate the Python program into C, line by line, plus a verification harness.

{C_CONVENTIONS}
{TAG_RULES.replace("Dafny", "C")}
Requirements:
- The entry function keeps its name `{entry.entry_point}`; it must be deterministic (no nondet_, no __CPROVER_).
- Also write `void nfv_harness(void)` (tag it `// synth: harness`): it creates nondeterministic inputs with
  `nondet_*()` functions you declare (e.g. `long long nondet_ll(void);`), bounds every length by NFV_MAX_LEN with
  `__CPROVER_assume`, then EXACTLY:
      __CPROVER_assume(nfv_pre(<params>));
      <ret type> return_value = {entry.entry_point}(<params>);
      __CPROVER_assert(nfv_spec(<params>, return_value), "nfv_spec");
  Declare (do not define) `bool nfv_pre(...)` and `bool nfv_spec(..., return_value)` above the harness; they are
  written by later stages.
- The library code below is prepended to yours; do not redefine it.

## Python program
{_python(entry)}

## Frozen library code
```c
{theory_code.strip()}
```

Answer with ONE fenced block:
```c
...
```"""


def c_precondition(entry: Entry, decl: str) -> str:
    return _stage("c.precondition") + f"""\
Stage 1b (C backend): define the precondition from the docstring only (you are not told the property).

```python
{entry.signature()}
    \"\"\"{entry.docstring}\"\"\"
```
The harness declares: `{decl}`
Define it so it returns true exactly for inputs the docstring allows (return true if it states no assumption).
It must be free of undefined behaviour.

{C_CONVENTIONS}
Answer with ONE fenced block containing the function definition:
```c
bool nfv_pre(...) {{ ... }}
```"""


def c_spec(entry: Entry, decl: str, theory_code: str) -> str:
    return _stage("c.spec") + f"""\
Stage 3 (C backend): translate the Python postcondition into the C predicate `nfv_spec`, faithfully.

```python
{entry.postcondition}
```
The harness declares: `{decl}` (the last parameter is the function's result `return_value`).
Type checks such as `isinstance(return_value, int)` hold by C typing: translate them to true. Use the library
code below (in scope) where it helps; write any further helper as a `static` function above `nfv_spec`.
It must be free of undefined behaviour.

{C_CONVENTIONS}
## Library code in scope
```c
{theory_code.strip()}
```

Answer with ONE fenced block containing the definitions:
```c
bool nfv_spec(...) {{ ... }}
```"""

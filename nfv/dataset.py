"""The nl2post-safe benchmark (paper Section 2 / Appendix A.1).

206 entries over 103 HumanEval tasks: each task contributes its reference
implementation (label ``correct``) and one mutant (label ``incorrect``), both
scored against the same Python postcondition ``S_L``.
"""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "data" / "nl2post_safe.jsonl"

CORRECT = "correct"
INCORRECT = "incorrect"


@dataclass
class Entry:
    id: str                 # e.g. "HumanEval_2/reference"
    task_id: str            # e.g. "HumanEval/2"
    entry_point: str        # function under test
    kind: str               # "reference" | "mutant"
    label: str              # "correct" | "incorrect"
    source: str             # Python module text (imports, helpers, function with docstring)
    docstring: str          # docstring of the entry point (S_nl / E_nl as given)
    postcondition: str      # sanitized S_L: Python assert over params and `return_value`
    tier: str = ""          # library tier L0/L1/L2a/L2b/L3 (Table 1)
    p: int = -1             # upstream postcondition response_num
    m: int = -1             # upstream mutant response_num
    killing_inputs: list = field(default_factory=list)  # mutants only: inputs from T the spec kills

    @property
    def task_num(self) -> int:
        return int(self.task_id.split("/")[1])

    @property
    def is_correct(self) -> bool:
        return self.label == CORRECT

    def to_json(self) -> dict:
        return asdict(self)

    def signature(self) -> str:
        """`def name(args) -> ret:` line of the entry point, as written."""
        fn = entry_function(self.source, self.entry_point)
        return source_line(self.source, fn.lineno).strip()


def load_entries(path: Path | str = DEFAULT_DATASET) -> list[Entry]:
    entries = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                entries.append(Entry(**json.loads(line)))
    return entries


def select(entries: list[Entry], ids: list[str] | None = None, tasks: list[int] | None = None,
           kind: str | None = None, limit: int | None = None) -> list[Entry]:
    out = entries
    if ids:
        wanted = set(ids)
        out = [e for e in out if e.id in wanted]
    if tasks:
        wanted_t = set(tasks)
        out = [e for e in out if e.task_num in wanted_t]
    if kind:
        out = [e for e in out if e.kind == kind]
    if limit is not None:
        out = out[:limit]
    return out


# ---------------------------------------------------------------- helpers


def entry_function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise ValueError(f"function {name!r} not found")


def source_line(source: str, lineno: int) -> str:
    return source.splitlines()[lineno - 1]


def numbered(source: str) -> str:
    """Source with `N | ` prefixes, the form the prompts quote and `src:` tags cite."""
    return "\n".join(f"{i:3d} | {l}" for i, l in enumerate(source.splitlines(), 1))

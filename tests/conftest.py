"""Shared fixtures. Nothing here talks to a real LLM: model output comes from the
sample responses under tests/fixtures/llm/<scenario>/<stage>.<n>.md."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfv.dataset import CORRECT, INCORRECT, Entry, load_entries, select  # noqa: E402
from nfv.llm import ScriptedBackend  # noqa: E402
from nfv.tools import Cbmc, Dafny  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"

SHIFTED_MEAN = '''def shifted_mean(l):
    "Average the list after adding 1 to each element; l must be non-empty."
    return sum(x + 1 for x in l) / len(l)
'''
SHIFTED_MEAN_BUG = '''def shifted_mean(l):
    "Average the list after adding 1 to each element; l must be non-empty."
    return (sum(l) + 1) / len(l)  # bug
'''


def paper_example(buggy: bool = False) -> Entry:
    """The running example of the paper's Section 1.2."""
    return Entry(id=f"paper/shifted_mean/{'mutant' if buggy else 'reference'}", task_id="paper/0",
                 entry_point="shifted_mean", kind="mutant" if buggy else "reference",
                 label=INCORRECT if buggy else CORRECT, source=SHIFTED_MEAN_BUG if buggy else SHIFTED_MEAN,
                 docstring="Average the list after adding 1 to each element; l must be non-empty.",
                 postcondition="assert return_value == sum(l) / len(l) + 1", tier="L1")


def scenario(name: str) -> dict[str, list[str]]:
    """stage -> responses in order (the last one repeats if the stage is asked again)."""
    out: dict[str, list[tuple[int, str]]] = {}
    for f in sorted((FIXTURES / name).glob("*.md")):
        stage, n, _ = f.name.rsplit(".", 2)
        out.setdefault(stage, []).append((int(n), f.read_text()))
    return {k: [t for _, t in sorted(v)] for k, v in out.items()}


def scripted(*names: str) -> ScriptedBackend:
    merged: dict[str, list[str]] = {}
    for n in names:
        for k, v in scenario(n).items():
            merged.setdefault(k, []).extend(v)
    return ScriptedBackend(merged)


def dataset_entry(entry_id: str) -> Entry:
    return select(load_entries(), ids=[entry_id])[0]


@pytest.fixture(scope="session")
def dafny() -> Dafny:
    d = Dafny(time_limit=30, cores=2)
    if not d.available:
        pytest.skip("dafny not on PATH (run the tests inside `nix develop`)")
    return d


@pytest.fixture(scope="session")
def cbmc() -> Cbmc:
    c = Cbmc(unwind=6, timeout=300)
    if not c.available:
        pytest.skip("cbmc not on PATH (run the tests inside `nix develop`)")
    return c

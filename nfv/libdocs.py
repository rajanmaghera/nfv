"""E_nl: the documentation of the library surface a program touches.

As in the paper's running example (``sum.__doc__``), the environment
description is the runtime docstring of each built-in, method or module
function the Python source calls.
"""

from __future__ import annotations

import ast
import builtins
import importlib

DOC_MODULES = {"math", "re", "functools", "itertools", "collections", "string", "heapq", "bisect", "statistics",
               "fractions", "decimal", "hashlib", "operator", "copy"}
METHOD_OWNERS = (str, list, dict, set, tuple, int, float, frozenset)


def _first_paragraphs(doc: str | None, max_chars: int = 900) -> str:
    if not doc:
        return ""
    doc = doc.strip()
    return doc if len(doc) <= max_chars else doc[:max_chars].rsplit("\n", 1)[0] + "\n..."


def library_surface(source: str) -> list[str]:
    """Qualified names of library entities the source uses, in first-use order."""
    tree = ast.parse(source)
    local = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                aliases[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                aliases[a.asname or a.name] = f"{n.module}.{a.name}"
    seen: list[str] = []

    def add(x: str):
        if x not in seen:
            seen.append(x)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) or isinstance(n, ast.Attribute) or isinstance(n, ast.Name):
            f = n.func if isinstance(n, ast.Call) else n
            if isinstance(f, ast.Name):
                if f.id in local:
                    continue
                if f.id in aliases and not aliases[f.id].startswith("typing"):
                    add(aliases[f.id])
                elif isinstance(n, ast.Call) and hasattr(builtins, f.id):
                    add(f.id)
            elif isinstance(f, ast.Attribute):
                if isinstance(f.value, ast.Name) and f.value.id in aliases:
                    mod = aliases[f.value.id]
                    if not mod.startswith("typing"):
                        add(f"{mod}.{f.attr}")
                elif isinstance(n, ast.Call):
                    add("." + f.attr)
    return seen


def doc_for(name: str) -> list[tuple[str, str]]:
    """[(qualified name, docstring)] for a surface entry."""
    out = []
    if name.startswith("."):
        meth = name[1:]
        for owner in METHOD_OWNERS:
            obj = getattr(owner, meth, None)
            if obj is not None and obj.__doc__:
                out.append((f"{owner.__name__}.{meth}", obj.__doc__))
        return out
    if "." in name:
        mod, attr = name.rsplit(".", 1)
        if mod.split(".")[0] in DOC_MODULES:
            try:
                obj = getattr(importlib.import_module(mod), attr, None)
            except ImportError:
                obj = None
            if obj is not None and obj.__doc__:
                out.append((name, obj.__doc__))
        return out
    obj = getattr(builtins, name, None)
    if obj is not None and obj.__doc__:
        out.append((name, obj.__doc__))
    return out


def library_docs(source: str) -> str:
    """Markdown-ish block of docstrings for the prompt (empty if nothing to model)."""
    parts = []
    for name in library_surface(source):
        for qual, doc in doc_for(name):
            parts.append(f"### {qual}\n{_first_paragraphs(doc)}")
    return "\n\n".join(parts)

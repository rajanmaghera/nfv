"""Library tiers L0-L3 (paper Table 1, Section 2.2).

A tier is a function of the calls the Python source makes -- a proxy for how
much of the environment E the LLM must formalize, not for algorithmic
difficulty:

  L0   nothing to axiomatize: arithmetic, indexing, control flow, len, range
  L1   total built-ins only (abs, sum, ord, ...)
  L2a  collections, first-order only (sorted, .sort, str/list/dict methods, ...)
  L2b  a function passed as an argument (filter, map, key=..., lambdas)
  L3   module imports, or any call outside the sets above

The paper does not publish the exact name lists; these are our reading of its
definitions, so per-tier counts can differ slightly from Table 1.
"""

from __future__ import annotations

import ast

L0_CALLS = {"len", "range", "print", "isinstance", "type"}
L1_CALLS = {"abs", "sum", "ord", "chr", "bool", "divmod", "pow", "round", "float", "int", "str", "hex", "oct", "bin"}
L2A_CALLS = {
    "sorted", "reversed", "list", "set", "dict", "tuple", "frozenset", "enumerate", "zip", "max", "min",
    "any", "all",
}
L2B_CALLS = {"map", "filter", "functools.reduce", "reduce"}
L2A_METHODS = {
    # list / dict / set
    "append", "extend", "insert", "pop", "remove", "sort", "reverse", "index", "count", "copy", "clear",
    "keys", "values", "items", "get", "add", "discard", "update", "setdefault",
    "intersection", "union", "difference", "symmetric_difference", "issubset", "issuperset",
    # str
    "split", "join", "strip", "lstrip", "rstrip", "lower", "upper", "replace", "startswith", "endswith",
    "isalpha", "isdigit", "isupper", "islower", "isspace", "isalnum", "isnumeric", "swapcase", "find",
    "rfind", "title", "capitalize", "zfill", "format", "center", "ljust", "rjust", "splitlines",
}
TYPING_MODULES = {"typing", "__future__"}

ORDER = ["L0", "L1", "L2a", "L2b", "L3"]


def _max(a: str, b: str) -> str:
    return a if ORDER.index(a) >= ORDER.index(b) else b


def classify_tier(source: str, entry_point: str | None = None) -> str:
    """Tier of a whole module (entry point plus the helpers it defines)."""
    tree = ast.parse(source)
    local_funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    tier = "L0"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m.split(".")[0] not in TYPING_MODULES for m in mods):
                tier = _max(tier, "L3")
        elif isinstance(node, ast.Lambda):
            tier = _max(tier, "L2b")
        elif isinstance(node, ast.Call):
            tier = _max(tier, _call_tier(node, local_funcs))
    return tier


def _passes_function(call: ast.Call, local_funcs: set[str]) -> bool:
    """A lambda, a `key=` argument, or a bare function name used as a value."""
    if any(k.arg == "key" for k in call.keywords):
        return True
    builtin_callables = L0_CALLS | L1_CALLS | L2A_CALLS
    for a in list(call.args) + [k.value for k in call.keywords]:
        if isinstance(a, ast.Lambda):
            return True
        if isinstance(a, ast.Name) and (a.id in local_funcs or a.id in builtin_callables):
            return True
    return False


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _call_tier(call: ast.Call, local_funcs: set[str]) -> str:
    if _passes_function(call, local_funcs) or _callee(call) in L2B_CALLS:
        return "L2b"
    f = call.func
    if isinstance(f, ast.Name):
        name = f.id
        if name in local_funcs or name in L0_CALLS:
            return "L0"
        if name in L1_CALLS:
            return "L1"
        if name in L2A_CALLS:
            return "L2a"
        return "L3"
    if isinstance(f, ast.Attribute):
        if f.attr in L2A_METHODS:
            return "L2a"
        return "L3"
    return "L3"

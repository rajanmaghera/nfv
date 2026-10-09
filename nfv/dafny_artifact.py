"""The Dafny artifact and the mechanical checks that keep the pipeline staged.

Layout of an artifact (assembled by the harness, never by the LLM)::

    // ==== NFV:THEORY BEGIN (stage 1a, frozen) ====
    ...library theory E_F (may contain {:axiom}s)...
    // ==== NFV:THEORY END ====
    // ==== NFV:SPEC BEGIN (stage 3, frozen) ====
    ...optional spec helper functions used by S_F...
    // ==== NFV:SPEC END ====
    // ==== NFV:PROGRAM BEGIN ====
    ...stage-2 translation P_F, every code line under a `// src:` or `// synth:` tag;
       the entry method carries the harness-inserted frozen contract:
         // synth: frozen precondition (stage 1b)
         requires ...
         // synth: frozen postcondition (stage 3)
         ensures ...
       ...and stage 4 appends `// synth: proof` lemmas, asserts, invariants...
    // ==== NFV:PROGRAM END ====

Checks (paper Section 3.1.1-3.1.2):

* provenance: untagged code is rejected; every ``src:`` tag quotes its Python
  line(s) verbatim;
* freeze: theory and spec regions byte-identical, the entry contract identical,
  every stage-2 ``src:`` tag still present in order, and the *ghost erasure* of
  the program region token-identical to the stage-2 translation (so proof
  search may add only ghost code: asserts, invariants, decreases, lemmas,
  lemma calls, calc, ghost vars);
* no unproven assumptions outside the theory: no ``assume``/``expect``,
  ``{:axiom}``, bodiless lemmas/functions, ``{:verify false}``, ``decreases *``,
  ``{:extern}``, ``include``...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARK = "// ==== NFV:{name} {what} ===="
REGIONS = ("THEORY", "SPEC", "PROGRAM")
REGION_TITLES = {"THEORY": "(stage 1a, frozen)", "SPEC": "(stage 3, frozen)", "PROGRAM": ""}

TAG_RE = re.compile(r"^\s*//\s*(src|synth)\s*:(.*)$")
SRC_RE = re.compile(r"^\s*source\.py:(\d+)(?:-(\d+))?\s*\|\s?(.*)$")

PRE_TAG = "// synth: frozen precondition (stage 1b)"
POST_TAG = "// synth: frozen postcondition (stage 3)"
WITNESS_TAG = "// synth: witness (refutation)"
NEGATED_POST_TAG = "// synth: frozen negated postcondition (refutation)"

# synth: kinds that stage 4 (proof search) may add, change or drop
PROOF_KINDS = {"proof", "invariant", "decreases", "lemma", "ghost", "assert", "termination"}

ALLOWED_ATTRIBUTES = {"induction", "trigger", "fuel", "opaque", "isolate_assertions", "vcs_split_on_every_assert",
                      "split_here", "focus", "resource_limit", "timeLimit", "timeLimitMultiplier", "auto_generated",
                      "nowarn", "matchingloop", "inductionTrigger", "rlimit", "disable_nonlinear_arithmetic"}

DECL_KEYWORDS = {"function", "predicate", "method", "lemma", "datatype", "codatatype", "type", "const", "newtype",
                 "class", "trait", "iterator", "module", "import", "constructor", "least", "greatest", "twostate",
                 "ghost", "opaque", "static", "abstract", "include"}
CLAUSE_KEYWORDS = {"requires", "ensures", "decreases", "reads", "modifies", "invariant", "yield"}
# a `{` right after one of these opens a set display / comprehension, not a body
# (`>` is deliberately absent: it usually closes a type such as `seq<T>`; `|` too: it usually closes a
#  cardinality `|s|`, as in `requires i < |s| {`)
EXPR_PREV = {"in", "then", "else", "return", ":=", "==", "!=", "<", "<=", ">=", "+", "-", "*", "/", "%",
             "&&", "||", "==>", "<==", "<==>", "!", "(", "[", ",", ":", "=>", "::", "!in", "requires",
             "ensures", "invariant", "decreases", "reads", "modifies", "assert", "assume", "expect", "var", "case",
             "{", ";", "multiset", "iset", "imap", "map", "set", "seq", "#"}


class ArtifactError(ValueError):
    pass


# ---------------------------------------------------------------- regions


def assemble(theory: str, spec: str, program: str) -> str:
    parts = []
    for name, body in (("THEORY", theory), ("SPEC", spec), ("PROGRAM", program)):
        title = REGION_TITLES[name]
        parts.append(MARK.format(name=name, what=("BEGIN " + title).strip()))
        parts.append(body.strip("\n"))
        parts.append(MARK.format(name=name, what="END"))
    return "\n".join(parts) + "\n"


def split_regions(text: str) -> dict[str, str]:
    out = {}
    for name in REGIONS:
        m = re.search(rf"^// ==== NFV:{name} BEGIN[^\n]*====\n(.*?)^// ==== NFV:{name} END ====", text, re.S | re.M)
        if not m:
            raise ArtifactError(f"region markers for {name} are missing or were edited")
        out[name] = m.group(1).rstrip("\n")
    return out


def _norm_lines(s: str) -> str:
    return "\n".join(l.rstrip() for l in s.strip("\n").splitlines())


# ---------------------------------------------------------------- tokens


@dataclass
class Tok:
    text: str
    line: int
    pos: int = 0      # offset of the first character in the source
    end: int = 0      # offset after the last character


_OPS = sorted(["<==>", "==>", "<==", ":=", ":|", "::", "==", "!=", "<=", ">=", "&&", "||", "..", "=>", "!in", "#["],
              key=len, reverse=True)


def tokenize(src: str) -> list[Tok]:
    """Dafny tokens (comments dropped). Good enough for structural checks."""
    toks = _TokList()
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            line += src.count("\n", i, j)
            i = j
            continue
        if c == '"' or (c == "@" and i + 1 < n and src[i + 1] == '"'):
            j = i + (2 if c == "@" else 1)
            verbatim = c == "@"
            while j < n:
                if not verbatim and src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    if verbatim and j + 1 < n and src[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok(src[i:j + 1], line, i, j + 1))
            line += src.count("\n", i, j + 1)
            i = j + 1
            continue
        if c == "'" and not (toks and re.match(r"[\w']$", toks[-1].text) and src[i - 1] not in " \t\n"):
            m = re.match(r"'(\\u\{?[0-9a-fA-F]+\}?|\\.|[^'\\])'", src[i:])
            if m:
                toks.append(Tok(m.group(0), line, i, i + len(m.group(0))))
                i += len(m.group(0))
                continue
        m = re.match(r"[A-Za-z_][\w'?]*", src[i:])
        if m:
            toks.append(Tok(m.group(0), line, i, i + len(m.group(0))))
            i += len(m.group(0))
            continue
        m = re.match(r"0x[0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:e-?\d+)?", src[i:])
        if m:
            toks.append(Tok(m.group(0), line, i, i + len(m.group(0))))
            i += len(m.group(0))
            continue
        for op in _OPS:
            if src.startswith(op, i):
                toks.append(Tok(op, line, i, i + len(op)))
                i += len(op)
                break
        else:
            toks.append(Tok(c, line, i, i + 1))
            i += 1
    toks.src = src
    return toks


class _TokList(list):
    """A token list that remembers its source text (for faithful re-rendering)."""
    src: str = ""


def render(toks: list[Tok], idx: list[int]) -> str:
    """Source text of the tokens at ``idx``: original spacing between adjacent tokens,
    one space where something (a comment, a dropped clause) was cut out."""
    src = getattr(toks, "src", "")
    out = []
    for k, i in enumerate(idx):
        if k:
            j = idx[k - 1]
            gap = src[toks[j].end:toks[i].pos] if src else " "
            out.append("" if i == j + 1 and gap == "" else " ")
        out.append(toks[i].text)
    return "".join(out)


def _match(toks: list[Tok], i: int, open_: str = "{", close: str = "}") -> int:
    """Index of the token closing the bracket opened at ``i``."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].text == open_:
            depth += 1
        elif toks[j].text == close:
            depth -= 1
            if depth == 0:
                return j
    raise ArtifactError(f"unbalanced {open_!r} opened on line {toks[i].line}")


def _skip_attribute(toks: list[Tok], i: int) -> int:
    """If toks[i] opens `{:attr ...}`, return the index after it; else i."""
    if toks[i].text == "{" and i + 1 < len(toks) and toks[i + 1].text == ":":
        return _match(toks, i) + 1
    return i


def attributes(toks: list[Tok]) -> list[tuple[str, int]]:
    out = []
    for i, t in enumerate(toks[:-2]):
        if t.text == "{" and toks[i + 1].text == ":" and re.match(r"\w", toks[i + 2].text):
            out.append((toks[i + 2].text, t.line))
    return out


# ---------------------------------------------------------------- declarations


@dataclass
class Decl:
    kind: str                 # function | predicate | method | lemma | datatype | const | type | ...
    name: str
    ghost: bool
    start: int                # token index range [start, end)
    end: int
    header_end: int           # index of the body `{` (== end if bodiless)
    has_body: bool
    line: int


def _is_body_brace(toks: list[Tok], i: int) -> bool:
    if toks[i].text != "{":
        return False
    if i + 1 < len(toks) and toks[i + 1].text == ":":
        return False  # attribute
    prev = toks[i - 1].text if i > 0 else ""
    return prev not in EXPR_PREV


def _paren_depth_scan(toks: list[Tok], i: int, stop) -> int:
    """Advance from i, skipping (), [], attributes and set displays, until stop(j) at depth 0."""
    depth = 0
    j = i
    while j < len(toks):
        t = toks[j].text
        if depth == 0 and stop(j):
            return j
        if t in "([":
            depth += 1
        elif t in ")]":
            depth -= 1
        elif t == "{":
            k = _skip_attribute(toks, j)
            if k != j:
                j = k
                continue
            if depth == 0 and _is_body_brace(toks, j):
                return j
            j = _match(toks, j) + 1
            continue
        j += 1
    return j


def declarations(toks: list[Tok]) -> list[Decl]:
    decls: list[Decl] = []
    i = 0
    n = len(toks)
    while i < n:
        start = i
        ghost = False
        # attributes and modifiers
        while i < n:
            k = _skip_attribute(toks, i)
            if k != i:
                i = k
                continue
            if toks[i].text in ("ghost", "opaque", "static", "least", "greatest", "twostate", "abstract"):
                ghost = ghost or toks[i].text in ("ghost", "least", "greatest", "twostate")
                i += 1
                continue
            break
        if i >= n:
            break
        kw = toks[i].text
        if kw == "function" and i + 1 < n and toks[i + 1].text == "method":
            i += 1  # Dafny 3 `function method`
        if kw in ("function", "predicate", "method", "lemma", "constructor", "iterator"):
            name_i = i + 1
            while name_i < n and toks[name_i].text == "{":
                name_i = _skip_attribute(toks, name_i)
            name = toks[name_i].text if name_i < n else "?"
            if kw == "lemma":
                ghost = True
            # header runs to the body brace or the next declaration keyword
            j = _paren_depth_scan(toks, name_i + 1,
                                  lambda j: toks[j].text in DECL_KEYWORDS and toks[j - 1].text not in (":", "."))
            if j < n and toks[j].text == "{":
                end = _match(toks, j) + 1
                decls.append(Decl(kw, name, ghost, start, end, j, True, toks[start].line))
            else:
                decls.append(Decl(kw, name, ghost, start, j, j, False, toks[start].line))
                end = j
            i = end
            continue
        # anything else (datatype, const, type, ...) runs to the next top-level declaration keyword
        name = toks[i + 1].text if i + 1 < n else "?"
        j = i + 1
        depth = 0
        while j < n:
            t = toks[j].text
            if t in "([{":
                depth += 1
            elif t in ")]}":
                depth -= 1
            elif depth == 0 and t in DECL_KEYWORDS and t not in ("type",):
                break
            j += 1
        decls.append(Decl(kw, name, ghost, start, j, j, j > i + 1, toks[start].line))
        i = j
    return decls


# ---------------------------------------------------------------- ghost erasure


def _erase_clauses(toks: list[Tok], i: int, end: int, out: list[str], errors: list[str]) -> None:
    """Copy toks[i:end] dropping requires/ensures/decreases/reads/modifies/invariant clauses and attributes."""
    j = i
    while j < end:
        t = toks[j].text
        k = _skip_attribute(toks, j)
        if k != j:
            j = k
            continue
        if t in CLAUSE_KEYWORDS:
            if t == "decreases" and j + 1 < end and toks[j + 1].text == "*":
                errors.append(f"line {toks[j].line}: `decreases *` (possible non-termination) is not allowed")
            j = _paren_depth_scan(toks, j + 1, lambda q: q >= end or toks[q].text in CLAUSE_KEYWORDS)
            continue
        out.append(t)
        j += 1


def _erase_body(toks: list[Tok], i: int, end: int, lemmas: set[str], out: list[str], errors: list[str]) -> None:
    """Copy a statement/expression body dropping ghost statements."""
    stmt_start = True
    j = i
    while j < end:
        t = toks[j].text
        k = _skip_attribute(toks, j)
        if k != j:
            j = k
            continue
        if stmt_start:
            if t in ("assert", "reveal", "label") or (t == "ghost" and j + 1 < end and toks[j + 1].text == "var"):
                j = _skip_stmt(toks, j, end)
                continue
            if t == "calc":
                b = next(q for q in range(j, end) if toks[q].text == "{" and _skip_attribute(toks, q) == q)
                j = _match(toks, b) + 1
                continue
            if t == "forall":
                b = _paren_depth_scan(toks, j + 1, lambda q: q >= end or toks[q].text in (";",))
                if b < end and toks[b].text == "{":
                    close = _match(toks, b)
                    if not any(toks[q].text in (":=", ":|") for q in range(b, close)):
                        j = close + 1
                        continue
            if re.match(r"^[A-Za-z_]", t) and t in lemmas and j + 1 < end and toks[j + 1].text == "(":
                close = _match(toks, j + 1, "(", ")")
                if close + 1 < end and toks[close + 1].text == ";":
                    j = close + 2
                    continue
        if t in ("while", "for"):
            out.append(t)
            # loop header: condition then clauses up to the body brace
            b = _paren_depth_scan(toks, j + 1, lambda q: q >= end or toks[q].text in CLAUSE_KEYWORDS)
            out.extend(x.text for x in toks[j + 1:b])
            c = _paren_depth_scan(toks, b, lambda q: q >= end)
            _erase_clauses(toks, b, c, out, errors)
            j = c
            stmt_start = False
            continue
        out.append(t)
        stmt_start = t in ("{", ";", "}", "else", "=>")
        j += 1


def _skip_stmt(toks: list[Tok], j: int, end: int) -> int:
    """From an assert/reveal/ghost var/label at j, return the index after it."""
    if toks[j].text == "label":
        return min(j + 3, end)
    depth = 0
    q = j
    while q < end:
        t = toks[q].text
        if t in "([":
            depth += 1
        elif t in ")]":
            depth -= 1
        elif t == "{":
            k = _skip_attribute(toks, q)
            if k != q:
                q = k
                continue
            q = _match(toks, q) + 1
            continue
        elif depth == 0 and t == ";":
            return q + 1
        elif depth == 0 and t == "by":
            b = q + 1
            return _match(toks, b) + 1
        q += 1
    return q


def ghost_erasure(program: str) -> tuple[list[tuple[str, str, tuple[str, ...]]], list[str]]:
    """[(kind, name, erased tokens)] for the non-ghost declarations, plus errors."""
    toks = tokenize(program)
    decls = declarations(toks)
    lemmas = {d.name for d in decls if d.kind == "lemma" or (d.ghost and d.kind == "method")}
    errors: list[str] = []
    out = []
    for d in decls:
        if d.ghost or d.kind == "lemma":
            continue
        buf: list[str] = []
        if d.kind in ("function", "predicate", "method", "constructor"):
            _erase_clauses(toks, d.start, d.header_end, buf, errors)
            if d.has_body:
                _erase_body(toks, d.header_end, d.end, lemmas, buf, errors)
        else:
            buf = [t.text for t in toks[d.start:d.end]]
        out.append((d.kind, d.name, tuple(buf)))
    return out, errors


# ---------------------------------------------------------------- entry method


def find_entry(toks: list[Tok], entry: str) -> Decl:
    for d in declarations(toks):
        if d.kind == "method" and d.name == entry:
            return d
    raise ArtifactError(f"no `method {entry}` found in the translation")


def entry_header(program: str, entry: str) -> str:
    """`method f(x: int) returns (return_value: int)` without clauses."""
    toks = tokenize(program)
    d = find_entry(toks, entry)
    keep = []
    j = d.start
    while j < d.header_end:
        k = _skip_attribute(toks, j)
        if k != j:
            j = k
            continue
        if toks[j].text in CLAUSE_KEYWORDS:
            j = _paren_depth_scan(toks, j + 1, lambda q: q >= d.header_end or toks[q].text in CLAUSE_KEYWORDS)
            continue
        keep.append(j)
        j += 1
    return render(toks, keep)


def entry_params(program: str, entry: str) -> str:
    """Parameter list text of the entry method, e.g. `l: seq<real>, k: int`."""
    toks = tokenize(program)
    d = find_entry(toks, entry)
    i = next(q for q in range(d.start, d.header_end) if toks[q].text == "(")
    close = _match(toks, i, "(", ")")
    return render(toks, list(range(i + 1, close)))


def entry_contract(program: str, entry: str) -> dict[str, list[str]]:
    toks = tokenize(program)
    d = find_entry(toks, entry)
    out: dict[str, list[str]] = {k: [] for k in CLAUSE_KEYWORDS}
    j = d.start
    while j < d.header_end:
        t = toks[j].text
        if t in CLAUSE_KEYWORDS:
            e = _paren_depth_scan(toks, j + 1, lambda q: q >= d.header_end or toks[q].text in CLAUSE_KEYWORDS)
            out[t].append(render(toks, list(range(j + 1, e))))
            j = e
            continue
        j += 1
    return out


def insert_contract(program: str, entry: str, requires: list[tuple[str, str]],
                    ensures: list[tuple[str, str]]) -> str:
    """Insert tagged `requires`/`ensures` lines right before the entry body brace.

    The body brace must be on its own line (the translation prompt asks for it)."""
    toks = tokenize(program)
    d = find_entry(toks, entry)
    if not d.has_body:
        raise ArtifactError(f"method {entry} has no body")
    brace_line = toks[d.header_end].line
    lines = program.splitlines()
    if lines[brace_line - 1].strip() != "{":
        raise ArtifactError(f"the opening brace of method {entry} must be on its own line")
    indent = "  "
    new = []
    for tag, expr in requires:
        new += [indent + tag, indent + "requires " + expr]
    for tag, expr in ensures:
        new += [indent + tag, indent + "ensures " + expr]
    return "\n".join(lines[:brace_line - 1] + new + lines[brace_line - 1:]) + "\n"


# ---------------------------------------------------------------- provenance


@dataclass
class TagLine:
    kind: str       # src | synth
    text: str       # everything after the colon
    line: int


def tag_lines(program: str) -> list[TagLine]:
    return [TagLine(m.group(1), m.group(2).strip(), i)
            for i, l in enumerate(program.splitlines(), 1) if (m := TAG_RE.match(l))]


def synth_kind(text: str) -> str:
    m = re.match(r"\s*([A-Za-z_-]+)", text)
    return m.group(1).lower() if m else ""


def check_provenance(program: str, python_source: str) -> list[str]:
    """Every code line sits under a tag; every src: tag quotes Python verbatim."""
    errors = []
    py = python_source.splitlines()
    code_lines = sorted({t.line for t in tokenize(program)})
    tags = tag_lines(program)
    if code_lines and (not tags or code_lines[0] < tags[0].line):
        errors.append(f"line {code_lines[0]}: untagged code (every line must follow a `// src:` or `// synth:` tag)")
    for t in tags:
        if t.kind == "synth":
            if not synth_kind(t.text):
                errors.append(f"line {t.line}: `// synth:` tag needs a kind, e.g. `// synth: declaration`")
            continue
        m = SRC_RE.match(t.text)
        if not m:
            errors.append(f"line {t.line}: malformed src tag; use `// src: source.py:N | <python line N verbatim>`")
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if not (1 <= a <= b <= len(py)):
            errors.append(f"line {t.line}: source.py:{a}-{b} is out of range (1..{len(py)})")
            continue
        want = " ".join(l.strip() for l in py[a - 1:b])
        got = m.group(3).strip()
        if re.sub(r"\s+", " ", want) != re.sub(r"\s+", " ", got):
            errors.append(f"line {t.line}: src tag does not quote source.py:{m.group(1)}"
                          f"{'-' + m.group(2) if m.group(2) else ''} verbatim; expected `{want}`")
    return errors


def python_coverage(program: str, python_source: str, entry: str) -> tuple[int, int]:
    """(# Python statement lines of the function quoted by some src tag, # statement lines)."""
    import ast
    tree = ast.parse(python_source)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == entry), None)
    if fn is None:
        return 0, 0
    stmt_lines = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.stmt) and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            stmt_lines.add(node.lineno)
    covered = set()
    for t in tag_lines(program):
        m = SRC_RE.match(t.text) if t.kind == "src" else None
        if m:
            a = int(m.group(1))
            covered.update(range(a, int(m.group(2) or a) + 1))
    return len(stmt_lines & covered), len(stmt_lines)


# ---------------------------------------------------------------- forbidden constructs


def check_forbidden(code: str, *, region: str) -> list[str]:
    """Constructs that would introduce unproven assumptions.

    The theory may contain `{:axiom}` declarations (that is what a library
    theory is); nothing else may.
    """
    errors = []
    toks = tokenize(code)
    for i, t in enumerate(toks):
        if t.text in ("assume", "expect") and not (i > 0 and toks[i - 1].text == ":"):
            errors.append(f"line {t.line}: `{t.text}` statements are not allowed")
        elif t.text in ("include", "import", "module", "refines", "extern") and not (i > 0 and toks[i - 1].text in (":", ".")):
            errors.append(f"line {t.line}: `{t.text}` is not allowed")
        elif t.text == "decreases" and i + 1 < len(toks) and toks[i + 1].text == "*":
            errors.append(f"line {t.line}: `decreases *` is not allowed")
    for name, line in attributes(toks):
        if name == "axiom" and region == "THEORY":
            continue
        if name == "verify" or name == "only" or name == "extern" or name == "axiom":
            errors.append(f"line {line}: attribute {{:{name}}} is not allowed here")
        elif name not in ALLOWED_ATTRIBUTES:
            errors.append(f"line {line}: attribute {{:{name}}} is not on the allow-list")
    for d in declarations(toks):
        if d.kind in ("function", "predicate", "lemma", "method") and not d.has_body:
            axiom = any(n == "axiom" for n, _ in attributes(toks[d.start:d.header_end]))
            if region != "THEORY" or not axiom:
                errors.append(f"line {d.line}: bodyless {d.kind} {d.name} is an unproven assumption"
                              + (" (theory axioms must carry {:axiom})" if region == "THEORY" else ""))
        if region == "THEORY" and d.kind == "method":
            errors.append(f"line {d.line}: the library theory must be pure (functions, predicates, lemmas, types)")
    return errors


AXIOM_WARNING_RE = re.compile(r"bodyless|assume statement|axiom", re.I)


def axiom_warnings(dafny_output_diags) -> list[str]:
    return [str(d) for d in dafny_output_diags if d.severity == "Warning" and AXIOM_WARNING_RE.search(d.message)]


# ---------------------------------------------------------------- the frozen check (stage 4)


@dataclass
class Frozen:
    """What proof search must leave untouched."""
    theory: str
    spec: str
    program: str             # stage-2 translation with the contract inserted
    entry: str
    requires: list[str]      # normalized clause texts
    ensures: list[str]
    src_tags: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, artifact: str, entry: str) -> "Frozen":
        r = split_regions(artifact)
        c = entry_contract(r["PROGRAM"], entry)
        return cls(theory=r["THEORY"], spec=r["SPEC"], program=r["PROGRAM"], entry=entry,
                   requires=c["requires"], ensures=c["ensures"],
                   src_tags=[t.text for t in tag_lines(r["PROGRAM"]) if t.kind == "src"])


def audit_proof_artifact(frozen: Frozen, artifact: str, python_source: str) -> list[str]:
    """All checks a repair round must pass before its proof counts."""
    try:
        r = split_regions(artifact)
    except ArtifactError as e:
        return [str(e)]
    errors = []
    if _norm_lines(r["THEORY"]) != _norm_lines(frozen.theory):
        errors.append("the library theory (THEORY region) is frozen and was modified")
    if _norm_lines(r["SPEC"]) != _norm_lines(frozen.spec):
        errors.append("the specification helpers (SPEC region) are frozen and were modified")
    prog = r["PROGRAM"]
    errors += check_forbidden(prog, region="PROGRAM")
    errors += check_forbidden(r["SPEC"], region="SPEC")
    errors += check_provenance(prog, python_source)
    try:
        c = entry_contract(prog, frozen.entry)
    except ArtifactError as e:
        return errors + [str(e)]
    if _toks(c["requires"]) != _toks(frozen.requires):
        errors.append("the precondition of the entry method is frozen; requires clauses were added, removed or edited")
    if _toks(c["ensures"]) != _toks(frozen.ensures):
        errors.append("the postcondition (goal) of the entry method is frozen; ensures clauses were edited")
    if c["modifies"] or c["reads"]:
        errors.append("the entry method may not gain reads/modifies clauses")
    tags_now = [t.text for t in tag_lines(prog) if t.kind == "src"]
    if not _is_subsequence(frozen.src_tags, tags_now) or len(tags_now) != len(frozen.src_tags):
        errors.append("`// src:` lines are frozen: proof search may not add, drop, reorder or edit them")
    for t in tag_lines(prog):
        if t.kind == "synth" and t.text not in _synth_texts(frozen.program) and synth_kind(t.text) not in PROOF_KINDS:
            errors.append(f"line {t.line}: new code from proof search must be tagged `// synth: proof` "
                          f"(or invariant/decreases/lemma/ghost); found `// synth: {t.text}`")
    try:
        before, e1 = ghost_erasure(frozen.program)
        after, e2 = ghost_erasure(prog)
    except ArtifactError as e:
        return errors + [f"could not parse the program region: {e}"]
    errors += e2
    errors += _compare_erasures(before, after)
    return errors


def _toks(clauses: list[str]) -> list[list[str]]:
    return [[t.text for t in tokenize(c)] for c in clauses]


def _synth_texts(program: str) -> set[str]:
    return {t.text for t in tag_lines(program) if t.kind == "synth"}


def _is_subsequence(a: list[str], b: list[str]) -> bool:
    it = iter(b)
    return all(x in it for x in a)


def _compare_erasures(before, after) -> list[str]:
    names_before = {(k, n) for k, n, _ in before}
    executable_refs = {tok for _, _, toks in before for tok in toks}
    # extra non-ghost helpers are tolerated only if nothing executable references them
    after_kept = [(k, n, t) for k, n, t in after if (k, n) in names_before or n in executable_refs]
    errors = []
    if [(k, n) for k, n, _ in after_kept] != [(k, n) for k, n, _ in before]:
        errors.append("the set or order of translated declarations changed during proof search")
        return errors
    for (k, n, t0), (_, _, t1) in zip(before, after_kept):
        if t0 != t1:
            diff_at = next((i for i, (a, b) in enumerate(zip(t0, t1)) if a != b), min(len(t0), len(t1)))
            ctx0 = " ".join(t0[max(0, diff_at - 6):diff_at + 6])
            ctx1 = " ".join(t1[max(0, diff_at - 6):diff_at + 6])
            errors.append(f"executable code of {k} {n} changed during proof search (only ghost code may be added): "
                          f"was `... {ctx0} ...`, now `... {ctx1} ...`")
    return errors


# ---------------------------------------------------------------- refutation helpers


def replace_entry_ensures(program: str, entry: str, new_clause: str) -> str:
    """Replace every `ensures` clause of the entry method with one clause (used by
    the reachability check: `ensures false` must NOT verify)."""
    toks = tokenize(program)
    d = find_entry(toks, entry)
    lines = program.splitlines()
    spans = []
    j = d.start
    while j < d.header_end:
        if toks[j].text == "ensures":
            e = _paren_depth_scan(toks, j + 1, lambda q: q >= d.header_end or toks[q].text in CLAUSE_KEYWORDS)
            spans.append((toks[j].line, toks[e - 1].line))
            j = e
            continue
        j += 1
    if not spans:
        raise ArtifactError("entry method has no ensures clause")
    drop = {ln for a, b in spans for ln in range(a, b + 1)}
    first = spans[0][0]
    out = []
    for i, l in enumerate(lines, 1):
        if i == first:
            out.append("  ensures " + new_clause)
        if i not in drop:
            out.append(l)
    return "\n".join(out) + "\n"

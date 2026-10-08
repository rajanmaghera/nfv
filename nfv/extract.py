"""Pulling code out of free-form LLM responses."""

from __future__ import annotations

import re
from dataclasses import dataclass

FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*([^\n`]*)\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


@dataclass
class Block:
    lang: str      # first word of the info string, lower-cased ("dafny", "c", "")
    label: str     # rest of the info string ("helpers", "ensures", ...)
    code: str


def strip_thinking(text: str) -> str:
    text = THINK_RE.sub("", text)
    # an unterminated <think> (truncated reasoning) leaves nothing usable before it
    if "<think>" in text.lower():
        text = text[: text.lower().index("<think>")]
    return text


def code_blocks(text: str) -> list[Block]:
    text = strip_thinking(text)
    out = []
    for m in FENCE_RE.finditer(text):
        info = m.group(2).strip().split(None, 1)
        lang = info[0].lower() if info else ""
        label = info[1].strip().lower() if len(info) > 1 else ""
        out.append(Block(lang=lang, label=label, code=m.group(3).rstrip("\n") + "\n"))
    return out


def extract_code(text: str, langs: tuple[str, ...] = ("dafny",), label: str | None = None) -> str | None:
    """The last fenced block in one of ``langs`` (optionally with ``label``).

    Falls back to an untagged fence, then to the whole response if it has no
    fence at all (small local models often omit them). Returns None if nothing
    usable is present.
    """
    blocks = code_blocks(text)
    langs_l = tuple(l.lower() for l in langs)
    cands = [b for b in blocks if b.lang in langs_l]
    if label is not None:
        cands = [b for b in cands if b.label == label]
    if cands:
        return cands[-1].code
    if label is not None:
        return None
    untagged = [b for b in blocks if b.lang == ""]
    if untagged:
        return untagged[-1].code
    if blocks:
        return None
    body = strip_thinking(text).strip()
    return body + "\n" if body else None


def extract_labeled(text: str, lang: str, labels: tuple[str, ...]) -> dict[str, str]:
    """All blocks of ``lang`` keyed by label (last one wins)."""
    out: dict[str, str] = {}
    for b in code_blocks(text):
        if b.lang == lang and b.label in labels:
            out[b.label] = b.code
    return out


def clauses(code: str, keyword: str) -> list[str]:
    """Split ``requires A\\n  && B\\nrequires C`` into ["A && B", "C"].

    Lines that do not start with ``keyword`` continue the previous clause;
    comments and blank lines are dropped; a lone ``none`` means no clauses.
    """
    out: list[str] = []
    for raw in code.splitlines():
        line = re.sub(r"//.*$", "", raw).strip()
        if not line or line.lower() in ("none", "(none)"):
            continue
        if re.match(rf"^{keyword}\b", line):
            out.append(line[len(keyword):].strip())
        elif out:
            out[-1] = (out[-1] + " " + line).strip()
        else:
            out.append(line)  # tolerate a bare expression with the keyword omitted
    return [c.rstrip(";").strip() for c in out if c.strip()]


VERDICT_RE = re.compile(r"\bVERDICT\s*[:=]\s*\**\s*(CORRECT|INCORRECT)\b", re.I)


def extract_verdict(text: str) -> str | None:
    """`VERDICT: CORRECT|INCORRECT` (last occurrence wins)."""
    hits = VERDICT_RE.findall(strip_thinking(text))
    return hits[-1].upper() if hits else None

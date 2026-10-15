"""Locating and running the external verifiers (Dafny, CBMC).

Binaries are taken from ``NFV_DAFNY`` / ``NFV_CBMC`` / ``NFV_GOTO_CC`` if set,
else from PATH (``nix develop`` puts them there).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import trace


def which(env: str, name: str) -> str | None:
    return os.environ.get(env) or shutil.which(name)


def dafny_bin() -> str | None:
    return which("NFV_DAFNY", "dafny")


def cbmc_bin() -> str | None:
    return which("NFV_CBMC", "cbmc")


def goto_cc_bin() -> str | None:
    return which("NFV_GOTO_CC", "goto-cc")


@dataclass
class Diag:
    line: int
    col: int
    severity: str   # Error | Warning | Related
    message: str

    def __str__(self) -> str:
        return f"line {self.line}:{self.col}: {self.severity}: {self.message}"


@dataclass
class DafnyResult:
    ok: bool
    exit_code: int
    output: str
    diags: list[Diag] = field(default_factory=list)
    verified: int = 0
    errors: int = 0
    timed_out: bool = False
    seconds: float = 0.0

    @property
    def error_diags(self) -> list[Diag]:
        return [d for d in self.diags if d.severity == "Error"]

    @property
    def warning_diags(self) -> list[Diag]:
        return [d for d in self.diags if d.severity == "Warning"]

    def feedback(self, limit: int = 4000) -> str:
        """Verifier output trimmed for a repair prompt."""
        text = self.output.strip()
        if self.timed_out:
            text += "\n(the verifier timed out)"
        return text[-limit:]


DIAG_RE = re.compile(r"^[^\s(]*\((\d+),(\d+)\): (Error|Warning|Related location|Info)\s*:?\s*(.*)$", re.M)
SUMMARY_RE = re.compile(r"finished with (\d+) verified, (\d+) errors?")


def parse_dafny_output(out: str) -> tuple[list[Diag], int, int]:
    diags = [Diag(int(m.group(1)), int(m.group(2)), m.group(3).split()[0], m.group(4).strip())
             for m in DIAG_RE.finditer(out)]
    m = SUMMARY_RE.search(out)
    verified, errors = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return diags, verified, errors


class Dafny:
    """``dafny resolve`` (syntax + types only) and ``dafny verify``."""

    def __init__(self, binary: str | None = None, time_limit: int = 30, process_timeout: int = 600,
                 cores: int | str = 2, extra_args: list[str] | None = None):
        self.binary = binary or dafny_bin()
        self.time_limit = time_limit
        self.cores = cores
        self.process_timeout = process_timeout
        self.extra_args = extra_args or []

    @property
    def available(self) -> bool:
        return bool(self.binary)

    def _run(self, cmd: str, code: str, args: list[str]) -> DafnyResult:
        if not self.binary:
            raise RuntimeError("dafny not found: run inside `nix develop` or set NFV_DAFNY")
        with tempfile.TemporaryDirectory(prefix="nfv-dafny-") as d:
            f = Path(d) / "artifact.dfy"
            f.write_text(code)
            t0 = time.monotonic()
            try:
                proc = subprocess.run([self.binary, cmd, "--use-basename-for-filename", *args, *self.extra_args,
                                       str(f)], capture_output=True, text=True, timeout=self.process_timeout, cwd=d)
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                return DafnyResult(False, -1, out, timed_out=True, seconds=time.monotonic() - t0)
        out = proc.stdout + proc.stderr
        diags, verified, errors = parse_dafny_output(out)
        res = DafnyResult(ok=proc.returncode == 0, exit_code=proc.returncode, output=out, diags=diags,
                          verified=verified, errors=errors, timed_out="timed out" in out,
                          seconds=time.monotonic() - t0)
        trace.emit("verifier", tool="dafny", command=f"dafny {cmd} {' '.join(args)} artifact.dfy", input=code,
                   output=out, exit_code=proc.returncode, ok=res.ok and not res.error_diags,
                   verified=verified, errors=errors, seconds=round(res.seconds, 2))
        return res

    def resolve(self, code: str) -> DafnyResult:
        # warnings are surfaced (and the audit rejects axiom warnings), not fatal here
        return self._run("resolve", code, ["--allow-warnings"])

    def verify(self, code: str, filter_symbol: str | None = None) -> DafnyResult:
        args = ["--allow-warnings", "--verification-time-limit", str(self.time_limit), "--cores", str(self.cores)]
        if filter_symbol:
            args.append(f"--filter-symbol={filter_symbol}")
        return self._run("verify", code, args)


# ---------------------------------------------------------------- CBMC


@dataclass
class CbmcProperty:
    id: str
    status: str             # SUCCESS | FAILURE | UNKNOWN
    description: str
    function: str
    trace_assignments: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CbmcResult:
    ok: bool                # cbmc ran to completion (independent of verdict)
    output: str
    properties: list[CbmcProperty] = field(default_factory=list)
    compile_error: str = ""
    timed_out: bool = False

    def failed(self) -> list[CbmcProperty]:
        return [p for p in self.properties if p.status == "FAILURE"]


class Cbmc:
    UB_CHECKS = ["--bounds-check", "--pointer-check", "--signed-overflow-check", "--conversion-check",
                 "--div-by-zero-check", "--pointer-overflow-check", "--undefined-shift-check"]

    def __init__(self, binary: str | None = None, goto_cc: str | None = None, unwind: int = 8,
                 timeout: int = 600, entry: str = "nfv_harness"):
        self.binary = binary or cbmc_bin()
        self.goto_cc = goto_cc or goto_cc_bin()
        self.unwind = unwind
        self.timeout = timeout
        self.entry = entry

    @property
    def available(self) -> bool:
        return bool(self.binary)

    def compile_check(self, code: str) -> str:
        """'' if the C translation unit type-checks, else the compiler output."""
        if not self.binary:
            raise RuntimeError("cbmc not found: run inside `nix develop` or set NFV_CBMC")
        with tempfile.TemporaryDirectory(prefix="nfv-cbmc-") as d:
            f = Path(d) / "artifact.c"
            f.write_text(code)
            if self.goto_cc:
                cmd = [self.goto_cc, "-c", str(f), "-o", str(Path(d) / "artifact.o")]
            else:
                cmd = [self.binary, str(f), "--show-symbol-table"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=d)
        if proc.returncode != 0:
            return (proc.stdout + proc.stderr).strip()[-4000:] or "compilation failed"
        return ""

    def run(self, code: str) -> CbmcResult:
        import json
        if not self.binary:
            raise RuntimeError("cbmc not found: run inside `nix develop` or set NFV_CBMC")
        with tempfile.TemporaryDirectory(prefix="nfv-cbmc-") as d:
            f = Path(d) / "artifact.c"
            f.write_text(code)
            cmd = [self.binary, str(f), "--function", self.entry, "--unwind", str(self.unwind),
                   "--no-unwinding-assertions", *self.UB_CHECKS, "--trace", "--json-ui"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, cwd=d)
            except subprocess.TimeoutExpired:
                return CbmcResult(False, "", timed_out=True)
        out = proc.stdout
        try:
            msgs = json.loads(out)
        except json.JSONDecodeError:
            return CbmcResult(False, out + proc.stderr, compile_error=(out + proc.stderr)[-4000:])
        props: list[CbmcProperty] = []
        errors = []
        for m in msgs:
            if m.get("messageType") == "ERROR":
                errors.append(m.get("messageText", ""))
            for r in m.get("result", []) or []:
                assigns = []
                for st in r.get("trace", []) or []:
                    if st.get("stepType") == "assignment" and st.get("assignmentType") == "variable" \
                            and st.get("sourceLocation", {}).get("function") == self.entry:
                        assigns.append((st.get("lhs", ""), str((st.get("value") or {}).get("data", ""))))
                pid = r.get("property", "")
                props.append(CbmcProperty(id=pid, status=r.get("status", "UNKNOWN"),
                                          description=r.get("description", ""),
                                          function=pid.split(".")[0], trace_assignments=assigns))
        if errors and not props:
            return CbmcResult(False, out, compile_error="\n".join(errors)[-4000:])
        return CbmcResult(True, out, properties=props)


def tool_versions() -> dict[str, str]:
    out = {}
    for name, b in (("dafny", dafny_bin()), ("cbmc", cbmc_bin())):
        if not b:
            out[name] = "missing"
            continue
        try:
            p = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=120)
            out[name] = (p.stdout or p.stderr).strip().splitlines()[0]
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {e}"
    return out

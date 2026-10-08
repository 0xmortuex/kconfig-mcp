"""Load a Kconfig tree: environment, shell handling, and undefined-variable tracking.

Kconfig trees are not self-contained. Linux's top-level Kconfig sources
`arch/$(SRCARCH)/Kconfig` and probes the compiler through `$(shell,...)`.
Kconfiglib expands an undefined variable to "" without complaint, so a
missing SRCARCH shows up later as a baffling "arch//Kconfig not found".
This loader records every undefined variable a tree references and names it
in the error.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from typing import Any

import kconfiglib

from .compat import ModernKconfig

# Linux's compiler-identity scripts, answered in dry mode. The values are
# plausible but fake; results always say shell commands were not run.
_DRY_INFO = {
    "cc-version.sh": "GCC 130200",
    "as-version.sh": "GNU 24200",
    "ld-version.sh": "GNU 24200",
    # The real scripts print 0 when rustc is absent.
    "rustc-version.sh": "0",
    "rustc-llvm-version.sh": "0",
}
_IF_SUCCESS_RE = re.compile(r'&&\s*echo\s+"?([^"|]*)"?\s*\|\|\s*echo\s+"?([^"]*)"?\s*$')
_SET_ECHO_RE = re.compile(r"^set --\s*(.*?)\s*&&\s*echo\s+\$(\d)\s*$")
_EXPR_RE = re.compile(r"^expr\s+(-?\d+)\s*([-+*/%])\s*(-?\d+)\s*$")

# Kconfiglib parses under process-wide state (os.environ), so loads run one
# at a time.
_LOAD_LOCK = threading.Lock()


class LoadError(RuntimeError):
    pass


class _Kconfig(ModernKconfig):
    """ModernKconfig plus shell-mode handling and undefined-variable tracking.

    The attributes are class-level because Kconfiglib parses inside
    __init__, before a subclass __init__ could set them. _LOAD_LOCK makes
    that safe.
    """

    shell_mode = "run"
    undefined_vars: set[str] = set()
    shell_log: list[str] = []
    suppressed_errors: list[str] = []

    def _fn_val(self, args: list[str]) -> str:
        fn = args[0]
        if fn == "shell" and fn not in self.variables:
            if len(args) != 2:
                return str(super()._fn_val(args))
            return _run_shell(self, args[1])
        if fn == "error-if" and self.shell_mode == "dry" and fn not in self.variables:
            if len(args) == 3 and args[1] == "y":
                # In dry mode the probes that feed error-if are guesses, so a
                # failure there says nothing about the tree. Record it instead.
                type(self).suppressed_errors.append(args[2])
            return ""
        # $(1), $(2)... are function parameters, not environment variables.
        if (len(args) == 1 and not fn.isdigit() and fn not in self.variables
                and fn not in self._functions and fn not in os.environ):
            type(self).undefined_vars.add(fn)
        return str(super()._fn_val(args))


def _run_shell(kconf: _Kconfig, command: str) -> str:
    cls = type(kconf)
    if cls.shell_mode == "dry":
        cls.shell_log.append(command)
        return _dry_shell(command)
    sh = shutil.which("sh")
    if sh is None:
        raise LoadError(
            "this Kconfig tree runs shell commands ($(shell,...)) and no POSIX `sh` was "
            "found on PATH. Install one (Git for Windows ships sh.exe) or load with "
            "shell=\"dry\" to skip them (compiler-dependent symbols then read as absent)."
        )
    proc = subprocess.run([sh, "-c", command], capture_output=True, text=True,
                          errors="replace")
    cls.shell_log.append(command)
    # Same post-processing as Kconfiglib's own _shell_fn: newlines become
    # spaces and trailing whitespace is dropped.
    return "\n".join(proc.stdout.splitlines()).replace("\n", " ").rstrip()


def _dry_shell(command: str) -> str:
    """Answer the shell idioms Kconfig trees use, without running anything."""
    cmd = command.strip()
    m = _IF_SUCCESS_RE.search(cmd)
    if m:
        # if-success/success/failure: assume the probed feature is absent.
        return m.group(2).strip()
    m = _SET_ECHO_RE.match(cmd)
    if m:
        words = m.group(1).split()
        idx = int(m.group(2)) - 1
        return words[idx] if 0 <= idx < len(words) else ""
    m = _EXPR_RE.match(cmd)
    if m:
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        if op in "/%" and b == 0:
            return "0"
        res = {"+": a + b, "-": a - b, "*": a * b, "/": int(a / b) if b else 0,
               "%": a % b if b else 0}[op]
        return str(res)
    for script, out in _DRY_INFO.items():
        if script in cmd:
            return out
    return ""


class Loaded:
    def __init__(self, kconf: kconfiglib.Kconfig, root: str, env: dict[str, str],
                 shell_mode: str, undefined: set[str], shell_cmds: int,
                 suppressed: list[str]):
        self.kconf = kconf
        self.root = root
        self.env = env
        self.shell_mode = shell_mode
        self.undefined_vars = undefined
        self.shell_cmds = shell_cmds
        self.suppressed_errors = suppressed


def load(kconfig_path: str, config_path: str | None, env: dict[str, str],
         shell: str = "run") -> Loaded:
    if shell not in ("run", "dry"):
        raise ValueError('shell must be "run" or "dry"')
    path = os.path.abspath(kconfig_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Kconfig file not found: {kconfig_path}")
    if config_path is not None and not os.path.isfile(config_path):
        raise FileNotFoundError(f".config file not found: {config_path}")
    srctree, fname = os.path.split(path)

    with _LOAD_LOCK:
        saved = dict(os.environ)
        try:
            os.environ.update({k: str(v) for k, v in env.items()})
            os.environ["srctree"] = srctree
            _Kconfig.shell_mode = shell
            _Kconfig.undefined_vars = set()
            _Kconfig.shell_log = []
            _Kconfig.suppressed_errors = []
            try:
                kconf: Any = _Kconfig(fname, warn_to_stderr=False)
            except kconfiglib.KconfigError as e:
                raise LoadError(_explain_failure(str(e), _Kconfig.undefined_vars, shell)) from e
            except OSError as e:
                raise LoadError(_explain_failure(str(e), _Kconfig.undefined_vars, shell)) from e
            if config_path is not None:
                kconf.load_config(os.path.abspath(config_path))
            return Loaded(kconf, path, dict(env), shell, set(_Kconfig.undefined_vars),
                          len(_Kconfig.shell_log), list(_Kconfig.suppressed_errors))
        finally:
            os.environ.clear()
            os.environ.update(saved)


_KNOWN_ENV = {
    "SRCARCH": "the arch/ directory to use, e.g. x86, arm64, riscv (Linux)",
    "ARCH": "the target architecture, e.g. x86, i386, x86_64, arm64 (Linux)",
    "KERNELVERSION": "only used in the menu title (Linux); any string works",
    "CC": "the C compiler command (Linux compiler probes)",
    "LD": "the linker command (Linux compiler probes)",
    "ZEPHYR_BASE": "path to the Zephyr tree (Zephyr)",
    "BOARD": "the board name (Zephyr)",
}


def _explain_failure(msg: str, undefined: set[str], shell: str) -> str:
    parts = [f"Kconfig failed to load: {msg}"]
    if undefined:
        hints = [f"{v} ({_KNOWN_ENV[v]})" if v in _KNOWN_ENV else v for v in sorted(undefined)]
        parts.append("The tree referenced undefined variable(s): " + ", ".join(hints)
                     + ". Pass them in env=, e.g. env={\"SRCARCH\": \"x86\", \"ARCH\": \"x86\"} "
                     "for Linux.")
    if shell == "run" and ("not supported" in msg or "not found" in msg and "compiler" in msg):
        parts.append("The tree's compiler probes failed. Either load from a full source "
                     "tree with the toolchain installed, or pass shell=\"dry\" to skip them.")
    return " ".join(parts)

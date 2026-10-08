"""Teach Kconfiglib the Kconfig syntax added after its last release (14.1.0, 2020).

Kconfiglib is the only full Python Kconfig implementation, but current Linux
uses three constructs it can't parse. Rather than fork it, we rewrite those
lines as files are read. Each rewrite keeps the line count unchanged, so every
file:line Kconfiglib reports still points at the real source line.

- `modules` (bare keyword): the same as `option modules`.
- `transitional`: marks a symbol that only exists to carry an old .config
  value across a rename. Kconfiglib has no equivalent, so the line is dropped
  and the symbol behaves as a promptless symbol with no default (always n).
  See README "Limitations".
- `depends on X if Y`: lowered exactly the way scripts/kconfig/menu.c does it,
  `X || <Y == n>`, where `<Y == n>` is built by a port of expr.c's
  expr_trans_compare(Y, E_EQUAL, &symbol_no). Note that kconfig itself
  lowers `!S == n` to `S != n` (not `S = y`), which differs when S is m. We
  match kconfig, not intuition.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import IO, Any, Union

import kconfiglib

# Property lines are indented with tabs only. Help text is indented further
# (tab + spaces), so anchoring on "^\t+keyword" never touches help paragraphs.
_MODULES_RE = re.compile(r"^(\t+)modules[ \t]*$")
_TRANSITIONAL_RE = re.compile(r"^\t+transitional[ \t]*$")
_DEPENDS_IF_RE = re.compile(r"^(\t+)depends on (.+?) if (.+?)[ \t]*$")


class UnsupportedSyntax(kconfiglib.KconfigError):  # type: ignore[misc]
    pass


# -- a minimal Kconfig expression parser, enough to lower `if <expr>` ---------

@dataclass
class Sym:
    name: str


@dataclass
class Not:
    operand: "Expr"


@dataclass
class Bin:
    op: str  # "&&" or "||"
    left: "Expr"
    right: "Expr"


@dataclass
class Cmp:
    op: str  # = != < <= > >=
    left: str
    right: str


Expr = Union[Sym, Not, Bin, Cmp]

_TOKEN_RE = re.compile(r'\s*(&&|\|\||!=|<=|>=|[!()=<>]|"(?:[^"\\]|\\.)*"|[A-Za-z0-9_$.\-]+)')


def _tokenize(text: str) -> list[str]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f"can't tokenize {text[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Parser:
    # Precedence, loosest first: || , && , ! , comparison , atom
    def __init__(self, text: str):
        self.toks = _tokenize(text)
        self.i = 0

    def parse(self) -> Expr:
        e = self._or()
        if self.i != len(self.toks):
            raise ValueError(f"unexpected {self.toks[self.i]!r}")
        return e

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self) -> str:
        tok = self._peek()
        if tok is None:
            raise ValueError("unexpected end of expression")
        self.i += 1
        return tok

    def _or(self) -> Expr:
        e = self._and()
        while self._peek() == "||":
            self._take()
            e = Bin("||", e, self._and())
        return e

    def _and(self) -> Expr:
        e = self._not()
        while self._peek() == "&&":
            self._take()
            e = Bin("&&", e, self._not())
        return e

    def _not(self) -> Expr:
        if self._peek() == "!":
            self._take()
            return Not(self._not())
        return self._cmp()

    def _cmp(self) -> Expr:
        if self._peek() == "(":
            self._take()
            e = self._or()
            if self._take() != ")":
                raise ValueError("missing ')'")
            return e
        left = self._take()
        if self._peek() in ("=", "!=", "<", "<=", ">", ">="):
            op = self._take()
            return Cmp(op, left, self._take())
        return Sym(left)


def _render(e: Expr) -> str:
    if isinstance(e, Sym):
        return e.name
    if isinstance(e, Not):
        return f"!({_render(e.operand)})"
    if isinstance(e, Bin):
        return f"({_render(e.left)} {e.op} {_render(e.right)})"
    return f"({e.left} {e.op} {e.right})"


def trans_compare_no(e: Expr, equal: bool) -> str:
    """Port of expr_trans_compare(e, equal ? E_EQUAL : E_UNEQUAL, &symbol_no)."""
    if isinstance(e, Bin):
        e1 = trans_compare_no(e.left, True)
        e2 = trans_compare_no(e.right, True)
        # sym == no: AND -> OR of the "== n" tests, OR -> AND of them.
        joined = f"({e1} || {e2})" if e.op == "&&" else f"({e1} && {e2})"
        return joined if equal else f"!{joined}"
    if isinstance(e, Not):
        return trans_compare_no(e.operand, not equal)
    if isinstance(e, Cmp):
        # A comparison is y or n, so (cmp == n) is !cmp and (cmp != n) is cmp.
        return f"!{_render(e)}" if equal else _render(e)
    return f"({e.name} = n)" if equal else f"({e.name} != n)"


def _strip_comment(line: str) -> str:
    """Drop a trailing # comment, respecting double-quoted strings."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_str = not in_str
        elif ch == "#" and not in_str:
            return line[:i].rstrip()
    return line


def rewrite_line(line: str, filename: str, linenr: int) -> str:
    """Rewrite one Kconfig line; returns it unchanged if nothing applies."""
    body = line.rstrip("\r\n")
    eol = line[len(body):]
    code = _strip_comment(body)
    m = _MODULES_RE.match(code)
    if m:
        return f"{m.group(1)}option modules{eol}"
    if _TRANSITIONAL_RE.match(code):
        return eol
    m = _DEPENDS_IF_RE.match(code)
    if m:
        indent, dep, cond = m.group(1), m.group(2), m.group(3)
        try:
            parsed = _Parser(cond).parse()
        except ValueError as e:
            raise UnsupportedSyntax(
                f"{filename}:{linenr}: can't parse the condition in 'depends on ... if "
                f"{cond}': {e}"
            ) from None
        # menu.c: "depends on X if Y" == X || (Y == n)
        return f"{indent}depends on ({dep}) || {trans_compare_no(parsed, True)}{eol}"
    return line


class ModernKconfig(kconfiglib.Kconfig):  # type: ignore[misc]
    """kconfiglib.Kconfig that parses current Linux Kconfig syntax."""

    def _open(self, filename: str, mode: str) -> IO[Any]:
        f = super()._open(filename, mode)
        # .config files are read through here too, but every rewrite needs a
        # tab-indented line and .config lines never start with a tab.
        if "r" not in mode:
            return f  # type: ignore[no-any-return]
        with f:
            lines = f.readlines()
        out = [rewrite_line(ln, filename, i) for i, ln in enumerate(lines, 1)]
        return io.StringIO("".join(out))

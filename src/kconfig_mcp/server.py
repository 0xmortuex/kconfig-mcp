"""kconfig-mcp: an MCP server that explains Kconfig - "why can't I enable CONFIG_X?"."""

from __future__ import annotations

import difflib
import functools
import os
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

import kconfiglib
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import engine
from .loader import Loaded, LoadError, load

mcp = MCPServer(
    "kconfig",
    instructions=(
        "Answer configuration questions about Kconfig trees (Linux kernel, Zephyr, U-Boot, "
        "Buildroot, ...). Start with kconfig_load (Linux needs env={\"SRCARCH\": \"x86\", "
        "\"ARCH\": \"x86\"}; pass shell=\"dry\" if the compiler isn't installed or on "
        "Windows). Then: kconfig_why_not explains every unmet dependency behind a symbol "
        "and proposes a VERIFIED minimal set of changes; kconfig_symbol shows one symbol; "
        "kconfig_set applies a change and reports what else changed; kconfig_save writes "
        "the .config. Symbol names work with or without the CONFIG_ prefix."
    ),
)

_sessions: dict[str, Loaded] = {}

F = TypeVar("F", bound=Callable[..., Any])

# Failures an agent can act on. MCPServer only forwards a ToolError's message
# to the client - anything else arrives as a bare "Error executing tool X" -
# so these are re-raised as ToolError with their text intact.
_EXPECTED = (LoadError, kconfiglib.KconfigError, KeyError, ValueError, FileNotFoundError,
             OSError)


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            # KeyError's str() wraps the message in quotes; use the raw text.
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            raise ToolError(str(msg)) from e

    mcp.tool()(wrapper)
    return fn


def _get(name: str) -> Loaded:
    s = _sessions.get(name)
    if s is None:
        known = ", ".join(sorted(_sessions)) or "none"
        raise KeyError(f"no Kconfig session named {name!r} (loaded: {known}) - "
                       f"call kconfig_load first")
    return s


def _sym(sess: Loaded, symbol: str) -> kconfiglib.Symbol:
    name = symbol.strip().removeprefix("CONFIG_")
    kconf = sess.kconf
    sym = kconf.syms.get(name)
    if sym is not None and (sym.nodes or not sym.is_constant):
        return sym
    names = [s.name for s in kconf.unique_defined_syms]
    close = difflib.get_close_matches(name, names, n=5, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise KeyError(f"no symbol {name!r} in this tree.{hint} (kconfig_search finds symbols "
                   f"by name, prompt or help text.)")


def _note_dry(sess: Loaded) -> str:
    if sess.shell_mode != "dry" or not sess.shell_cmds:
        return ""
    return (f"\n(note: loaded with shell=\"dry\" - {sess.shell_cmds} $(shell) probes were not "
            f"run, so compiler-dependent symbols such as CC_HAS_* read as absent)")


@tool
def kconfig_load(name: str, kconfig_path: str, config_path: str | None = None,
                 env: dict[str, str] | None = None, shell: str = "run") -> str:
    """Parse a Kconfig tree (and optionally a .config) into a session named `name`.

    kconfig_path is the top-level Kconfig file; its directory becomes srctree.
    env supplies the variables the tree expands - Linux needs SRCARCH and ARCH
    (e.g. {"SRCARCH": "x86", "ARCH": "x86_64"}); Zephyr needs ZEPHYR_BASE and
    friends. Missing ones are named in the error.
    shell: "run" executes the tree's $(shell,...) compiler probes with a POSIX sh,
    as `make menuconfig` would. "dry" runs nothing (no compiler needed, works on
    Windows); compiler-feature symbols then read as absent.
    A Linux tree takes 10-40 s to parse; the session stays loaded.
    """
    if name in _sessions:
        raise ValueError(f"a session named {name!r} is already loaded - kconfig_unload it first "
                         f"or pick another name")
    t0 = time.monotonic()
    sess = load(kconfig_path, config_path, env or {}, shell)
    _sessions[name] = sess
    k = sess.kconf
    lines = [
        f"Loaded {name!r}: {k.mainmenu_text} - {len(k.unique_defined_syms)} symbols, "
        f"{len(k.unique_choices)} choices ({time.monotonic() - t0:.1f}s).",
    ]
    if config_path:
        lines.append(f".config: {config_path}"
                     + (f" ({len(k.missing_syms)} assignments to symbols not in this tree)"
                        if k.missing_syms else ""))
    if sess.undefined_vars:
        lines.append("Undefined variables (expanded to \"\"): " + ", ".join(sorted(sess.undefined_vars)))
    if sess.suppressed_errors:
        lines.append("Ignored $(error-if) checks in dry mode: " + "; ".join(sess.suppressed_errors))
    warn = [w for w in k.warnings if "leading or trailing whitespace" not in w]
    if warn:
        lines.append(f"{len(warn)} Kconfig warnings, e.g.: {warn[0][:200]}")
    return "\n".join(lines) + _note_dry(sess)


@tool
def kconfig_unload(name: str) -> str:
    """Forget a loaded session."""
    _get(name)
    del _sessions[name]
    return f"Unloaded {name!r}."


@tool
def kconfig_sessions() -> str:
    """List loaded sessions."""
    if not _sessions:
        return "No Kconfig sessions loaded."
    return "\n".join(
        f"{n}: {s.root} ({len(s.kconf.unique_defined_syms)} symbols, env={s.env}, "
        f"shell={s.shell_mode})" for n, s in _sessions.items())


@tool
def kconfig_symbol(name: str, symbol: str) -> str:
    """Everything about one symbol: type, prompt, current value and why, help
    text, where it's defined, its dependencies, and what selects/implies it."""
    sess = _get(name)
    s = _sym(sess, symbol)
    out = [f"CONFIG_{s.name} = {engine.value_str(s)}  ({engine.how_set(s)})",
           f"type: {engine.type_str(s)}" + (f", choice member of {engine.loc(s.choice)}"
                                           if s.choice else "")]
    for n in s.nodes:
        prompt = f'"{n.prompt[0]}"' if n.prompt else "(no prompt)"
        out.append(f"defined at {n.filename}:{n.linenr} {prompt}")
    if engine.is_bool_like(s):
        out.append(f"assignable values now: {', '.join(engine.TRI_STR[v] for v in s.assignable) or 'none (hidden)'}")
    out.append(f"depends on: {kconfiglib.expr_str(s.direct_dep)} "
               f"(= {engine.TRI_STR[kconfiglib.expr_value(s.direct_dep)]})")
    for val, cond in s.defaults[:5]:
        out.append(f"default {kconfiglib.expr_str(val)}"
                   + ("" if cond is sess.kconf.y else f" if {kconfiglib.expr_str(cond)}"))
    if s.rev_dep is not sess.kconf.n:
        out.append(f"selected by: {kconfiglib.expr_str(s.rev_dep)} "
                   f"(= {engine.TRI_STR[kconfiglib.expr_value(s.rev_dep)]})")
    if s.weak_rev_dep is not sess.kconf.n:
        out.append(f"implied by: {kconfiglib.expr_str(s.weak_rev_dep)}")
    if s.selects:
        out.append("selects: " + ", ".join(t.name for t, _ in s.selects))
    if s.implies:
        out.append("implies: " + ", ".join(t.name for t, _ in s.implies))
    helps = [n.help for n in s.nodes if n.help]
    if helps:
        h = helps[0].strip()
        out.append("help:\n  " + (h[:1500] + "..." if len(h) > 1500 else h).replace("\n", "\n  "))
    return "\n".join(out)


@tool
def kconfig_why_not(name: str, symbol: str, value: str = "y") -> str:
    """Explain why a symbol isn't (or can't be) at `value`, and how to fix it.

    Walks every unmet `depends on`, `visible if`, `if` block, choice and
    select/default condition with file:line, recursing into the symbols they
    name. Then searches for a minimal set of assignments and VERIFIES it by
    applying it to the tree - a fix is only shown if Kconfig agrees it works,
    along with its side effects. value: "y", "m", or "n" (why can't I turn it off?).
    """
    sess = _get(name)
    s = _sym(sess, symbol)
    if value not in engine.TRI:
        raise ValueError('value must be "y", "m" or "n"; for int/string symbols use '
                         'kconfig_symbol and kconfig_set')
    if not engine.is_bool_like(s):
        raise ValueError(f"{s.name} is a {engine.type_str(s)} symbol - why_not reasons about "
                         f"bool/tristate symbols; use kconfig_symbol to see its dependencies")
    need = engine.TRI[value]
    if need == 1 and s.orig_type == kconfiglib.BOOL:
        raise ValueError(f"{s.name} is a bool, so it can't be m")
    reached = s.tri_value == need if need in (0, 1) else s.tri_value >= need
    if reached:
        return f"CONFIG_{s.name} is already {s.str_value} ({engine.how_set(s)})."
    ex = engine.Explainer(sess.kconf)
    if need == 0:
        ex.why_not_off(s)
    else:
        ex.why_not(s, need)
    out = ["Why not:", *ex.lines]
    if ex.truncated:
        out.append(f"  ... (explanation truncated at {ex.max_lines} lines)")
    plans, exhausted = engine.plan_for(sess.kconf, s, need)
    result = engine.verify(sess.kconf, s, need, plans)
    out.append("")
    if result is None:
        why = ("the search budget ran out before a working combination was found"
               if exhausted else "every route ends in something no assignment can change "
               "(see the explanation above)")
        out.append(f"No verified fix: {why}. Searched {len(plans)} candidate plan(s).")
    else:
        plan, effects = result
        assign = ", ".join(f"{k}={v}" for k, v in plan.items())
        out.append(f"Verified fix ({len(plan)} change(s)): {assign}")
        for k in plan:
            out.append(f"  {k}: {engine.loc(sess.kconf.syms[k])}")
        if effects:
            shown = list(effects.items())[:15]
            out.append(f"Side effects ({len(effects)} other symbol(s) change): "
                       + ", ".join(f"{k} {a}->{b}" for k, (a, b) in shown)
                       + (" ..." if len(effects) > 15 else ""))
        out.append("(not applied - call kconfig_set for each assignment, or apply them in order)")
        alt = [p for p in plans if p != plan][:3]
        if alt:
            out.append("Unverified alternatives: " + " | ".join(
                ", ".join(f"{k}={v}" for k, v in p.items()) for p in alt))
    return "\n".join(out) + _note_dry(sess)


@tool
def kconfig_what_selects(name: str, symbol: str) -> str:
    """Who forces this symbol on (select), who suggests it (imply), and what it
    selects/implies itself - with current values and locations."""
    sess = _get(name)
    s = _sym(sess, symbol)
    k = sess.kconf
    out = [f"CONFIG_{s.name} = {engine.value_str(s)}"]

    def users(attr: str) -> list[str]:
        rows = []
        for other in k.unique_defined_syms:
            for target, cond in getattr(other, attr):
                if target is s:
                    active = other.tri_value and kconfiglib.expr_value(cond)
                    c = "" if cond is k.y else f" if {kconfiglib.expr_str(cond)}"
                    rows.append(f"  {other.name} = {engine.value_str(other)}{c} "
                                f"{'[ACTIVE] ' if active else ''}at {engine.loc(other)}")
        return rows

    sel = users("selects")
    imp = users("implies")
    out.append(f"selected by ({len(sel)}):" if sel else "selected by: nothing")
    out.extend(sel[:40])
    if len(sel) > 40:
        out.append(f"  ... and {len(sel) - 40} more")
    out.append(f"implied by ({len(imp)}):" if imp else "implied by: nothing")
    out.extend(imp[:20])
    if s.selects:
        out.append("it selects: " + ", ".join(t.name for t, _ in s.selects))
    if s.implies:
        out.append("it implies: " + ", ".join(t.name for t, _ in s.implies))
    return "\n".join(out)


@tool
def kconfig_search(name: str, query: str, limit: int = 20) -> str:
    """Find symbols whose name, prompt or help text matches `query`
    (case-insensitive; a regex is accepted). Name matches rank first."""
    sess = _get(name)
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    try:
        rx = re.compile(query, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"bad regex {query!r}: {e}") from None
    hits: list[tuple[int, str]] = []
    for s in sess.kconf.unique_defined_syms:
        prompts = [n.prompt[0] for n in s.nodes if n.prompt]
        helps = [n.help for n in s.nodes if n.help]
        if rx.search(s.name):
            rank = 0
        elif any(rx.search(p) for p in prompts):
            rank = 1
        elif any(rx.search(h) for h in helps):
            rank = 2
        else:
            continue
        p = f' "{prompts[0]}"' if prompts else ""
        hits.append((rank, f"{s.name} = {engine.value_str(s)}{p}  [{engine.loc(s)}]"))
    hits.sort(key=lambda h: (h[0], h[1]))
    if not hits:
        return f"No symbols match {query!r}."
    more = f"\n... {len(hits) - limit} more (raise limit or narrow the query)" if len(hits) > limit else ""
    return f"{len(hits)} match(es):\n" + "\n".join(h for _, h in hits[:limit]) + more


@tool
def kconfig_set(name: str, symbol: str, value: str) -> str:
    """Assign a value (y/m/n for bool/tristate; a number or string otherwise)
    as if in menuconfig, then report everything else that changed as a result.
    If the value doesn't stick (hidden prompt, unmet dependency, choice rules)
    it says so - use kconfig_why_not to see why."""
    sess = _get(name)
    s = _sym(sess, symbol)
    before = engine.values(sess.kconf)
    old = s.str_value
    s.set_value(value)
    after = engine.values(sess.kconf)
    changed = {k: (before.get(k, ""), v) for k, v in after.items()
               if before.get(k) != v and k != s.name}
    if s.str_value != value:
        msg = (f"CONFIG_{s.name} did NOT become {value!r}: it is {engine.value_str(s)} "
               f"(was {old!r}). Kconfig refused or clamped the assignment - "
               f"kconfig_why_not {s.name} {value} explains why.")
        remembered = s.user_value
        if engine.is_bool_like(s) and isinstance(remembered, int):
            remembered = engine.TRI_STR[remembered]
        if remembered == value:
            msg += (" The request is remembered, as it would be from a .config, and takes "
                    "effect as soon as the dependencies allow it.")
    else:
        msg = f"CONFIG_{s.name}: {old!r} -> {value!r}."
    if changed:
        shown = list(changed.items())[:30]
        msg += f"\n{len(changed)} other symbol(s) changed as a consequence: " + ", ".join(
            f"{k} {a}->{b}" for k, (a, b) in shown) + (" ..." if len(changed) > 30 else "")
    return msg


_CFG_LINE = re.compile(r"^CONFIG_([A-Za-z0-9_]+)=(.*)$")
_CFG_UNSET = re.compile(r"^# CONFIG_([A-Za-z0-9_]+) is not set$")


def _parse_config(path: str) -> dict[str, str]:
    vals: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            m = _CFG_LINE.match(line)
            if m:
                v = m.group(2)
                vals[m.group(1)] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
                continue
            m = _CFG_UNSET.match(line)
            if m:
                vals[m.group(1)] = "n"
    return vals


@tool
def kconfig_diff(name: str, other_config: str, fragment: bool = False, limit: int = 100) -> str:
    """Compare the session's current values with another config file.

    A full .config lists every symbol that has a value, so a bool/tristate
    missing from it is n - that's the default. For a defconfig or a config
    fragment (only the interesting lines), pass fragment=True to compare
    just the symbols the file mentions.
    """
    sess = _get(name)
    if not os.path.isfile(other_config):
        raise FileNotFoundError(f".config not found: {other_config}")
    other = _parse_config(other_config)
    k = sess.kconf
    rows = []
    for s in k.unique_defined_syms:
        if not s.nodes:
            continue
        mine = s.str_value
        theirs = other.get(s.name)
        if theirs is None:
            if engine.is_bool_like(s) and not fragment:
                theirs = "n"
            else:
                continue
        if engine.is_bool_like(s) and mine == "":
            mine = "n"
        if mine != theirs:
            shown_mine = mine if mine != "" else '""'
            rows.append(f"{s.name}: {shown_mine} here, {theirs} in {os.path.basename(other_config)}")
    unknown = sorted(n for n in other if n not in k.syms or not k.syms[n].nodes)
    out = [f"{len(rows)} difference(s):"] + rows[:limit]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more (raise limit)")
    if unknown:
        out.append(f"{len(unknown)} symbol(s) in {os.path.basename(other_config)} don't exist in "
                   f"this tree: {', '.join(unknown[:15])}" + (" ..." if len(unknown) > 15 else ""))
    return "\n".join(out)


@tool
def kconfig_save(name: str, path: str, minimal: bool = False) -> str:
    """Write the current configuration. minimal=True writes a defconfig-style
    file with only the values that differ from defaults."""
    sess = _get(name)
    target = os.path.abspath(path)
    if minimal:
        msg = sess.kconf.write_min_config(target)
    else:
        msg = sess.kconf.write_config(target)
    return str(msg)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

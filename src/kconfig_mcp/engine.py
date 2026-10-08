"""Reasoning over a loaded Kconfig tree: explain blockers, plan fixes, apply changes.

The core question is "why can't SYMBOL be set to VALUE?". That has two halves:

- explain(): walk the symbol's prompt conditions, select/imply terms, and
  defaults, and report every unmet condition with file:line, recursing into
  the symbols those conditions name.
- plan(): search for a small set of user assignments that would make it
  possible, then *verify* each candidate by applying it to the real tree
  and checking the result. A plan is only reported if Kconfiglib agrees it
  works, because Kconfig semantics are subtle enough (choices, tristate, select
  overriding depends) that a plausible-looking plan can be wrong.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import kconfiglib
from kconfiglib import AND, NOT, OR, Choice, Symbol, expr_str, expr_value, split_expr

TRI = {"n": 0, "m": 1, "y": 2}
TRI_STR = {0: "n", 1: "m", 2: "y"}
CMP_OPS = (kconfiglib.EQUAL, kconfiglib.UNEQUAL, kconfiglib.LESS, kconfiglib.LESS_EQUAL,
           kconfiglib.GREATER, kconfiglib.GREATER_EQUAL)

MAX_DEPTH = 6
MAX_PLANS = 24      # candidate plans kept per sub-goal
MAX_VERIFY = 40     # candidate plans actually simulated

Plan = dict[str, str]


def loc(sc: Symbol | Choice) -> str:
    nodes = sc.nodes
    if not nodes:
        return "not defined in this tree"
    return ", ".join(f"{n.filename}:{n.linenr}" for n in nodes[:3]) + (
        f" (+{len(nodes) - 3} more)" if len(nodes) > 3 else "")


def type_str(sym: Symbol) -> str:
    return str(kconfiglib.TYPE_TO_STR[sym.orig_type])


def is_bool_like(sym: Symbol) -> bool:
    return bool(sym.orig_type in (kconfiglib.BOOL, kconfiglib.TRISTATE))


def has_prompt(sym: Symbol) -> bool:
    return any(n.prompt for n in sym.nodes)


def value_str(sym: Symbol) -> str:
    return str(sym.str_value) if sym.str_value != "" else '""'


def how_set(sym: Symbol) -> str:
    """Why does this symbol have its current value? One short phrase."""
    if sym.is_constant:
        return "constant"
    if not sym.nodes:
        return "not defined in this tree (always n)"
    parts = []
    if sym.user_value is not None and has_prompt(sym):
        parts.append(f"user set {sym.user_value if isinstance(sym.user_value, str) else TRI_STR.get(sym.user_value, sym.user_value)}")
    if is_bool_like(sym) and expr_value(sym.rev_dep):
        sel = [s.name for s in _true_selectors(sym)]
        parts.append("selected by " + ", ".join(sel[:4]) + ("..." if len(sel) > 4 else ""))
    if not parts:
        parts.append("default" if sym.nodes else "")
    return "; ".join(p for p in parts if p)


def _true_selectors(sym: Symbol) -> list[Symbol]:
    out = []
    for term in split_expr(sym.rev_dep, OR):
        if expr_value(term):
            for s in _syms_in(term):
                if s.choice is None and not s.is_constant and s.tri_value and any(
                        t is sym for t, _ in s.selects):
                    out.append(s)
    return out


def _syms_in(e: Any) -> list[Symbol]:
    if isinstance(e, (Symbol, Choice)):
        return [e] if isinstance(e, Symbol) else []
    out: list[Symbol] = []
    for part in e[1:]:
        out.extend(_syms_in(part))
    return out


# -- explanation -------------------------------------------------------------

class Explainer:
    def __init__(self, kconf: kconfiglib.Kconfig, max_depth: int = 14, max_lines: int = 150):
        # Depth is indentation: each symbol hop adds 2, so 14 is about 7 hops.
        self.kconf = kconf
        self.max_depth = max_depth
        self.max_lines = max_lines
        self.truncated = False
        self.seen: set[tuple[str, int]] = set()
        self.lines: list[str] = []

    def emit(self, depth: int, text: str) -> None:
        # Real trees fan out (an OR of 30 alternatives, each with its own deps),
        # so cap the report and say so rather than flooding the agent.
        if len(self.lines) >= self.max_lines:
            self.truncated = True
            return
        self.lines.append("  " * depth + text)

    def why_not(self, sym: Symbol, need: int, depth: int = 0) -> None:
        if need == 1 and sym.orig_type == kconfiglib.BOOL:
            need = 2  # kconfig promotes m to y for bool symbols
        key = (sym.name, need)
        header = (f"{sym.name} = {value_str(sym)} ({how_set(sym)}), needs {TRI_STR[need]} - "
                  f"{type_str(sym)} at {loc(sym)}")
        if key in self.seen:
            self.emit(depth, f"{sym.name}: see above")
            return
        self.seen.add(key)
        self.emit(depth, header)
        if depth >= self.max_depth:
            self.emit(depth + 1, "(stopping here: depth limit)")
            return
        if not sym.nodes:
            self.emit(depth + 1, f"{sym.name} is never defined in this tree, so it is always n. "
                                 f"It probably belongs to another architecture or a "
                                 f"subsystem whose Kconfig isn't sourced here.")
            return
        if sym.choice is not None and need == 2:
            self._choice(sym, depth + 1)
            return
        if need == 1 and sym.orig_type == kconfiglib.TRISTATE and self.kconf.modules.tri_value < 2:
            self.emit(depth + 1, f"m requires module support: {self.kconf.modules.name} = "
                                 f"{self.kconf.modules.str_value}")
            self.why_not(self.kconf.modules, 2, depth + 2)
        prompts = [n for n in sym.nodes if n.prompt]
        if prompts:
            vis = [n for n in prompts if expr_value(n.prompt[1]) >= need]
            if not vis:
                for n in prompts:
                    cond = n.prompt[1]
                    self.emit(depth + 1, f"its prompt ({n.filename}:{n.linenr}) is visible only if "
                                         f"{expr_str(cond)}, which is "
                                         f"{TRI_STR[expr_value(cond)]}:")
                    self._unmet(cond, need, depth + 2)
            else:
                self.emit(depth + 1, f"its prompt is visible - set {sym.name}={TRI_STR[need]} directly.")
        else:
            self.emit(depth + 1, f"{sym.name} has no prompt, so it can't be set directly. "
                                 f"It only gets a value from 'select' or a default:")
            terms = [t for t in split_expr(sym.rev_dep, OR) if t is not self.kconf.n]
            if terms:
                for t in terms[:8]:
                    self.emit(depth + 2, f"select via {expr_str(t)} = {TRI_STR[expr_value(t)]}"
                                         + self._sel_loc(t, sym))
                if len(terms) > 8:
                    self.emit(depth + 2, f"... and {len(terms) - 8} more selectors")
            else:
                self.emit(depth + 2, "nothing in this tree selects it")
            for val, cond in sym.defaults[:6]:
                self.emit(depth + 2, f"default {expr_str(val)} if {expr_str(cond)} "
                                     f"(condition = {TRI_STR[expr_value(cond)]})")
            if not sym.defaults:
                self.emit(depth + 2, "no defaults")
            if expr_value(sym.direct_dep) < need:
                self.emit(depth + 2, f"and its own dependencies ({expr_str(sym.direct_dep)}) are "
                                     f"{TRI_STR[expr_value(sym.direct_dep)]}, which caps any default:")
                self._unmet(sym.direct_dep, need, depth + 3)
        if expr_value(sym.direct_dep) < need and prompts and is_bool_like(sym):
            sel = [t for t in split_expr(sym.rev_dep, OR) if t is not self.kconf.n]
            if sel:
                self.emit(depth + 1, "it could also be forced on by 'select' (bypassing its "
                                     "dependencies) via: " + ", ".join(expr_str(t) for t in sel[:5]))

    def why_not_off(self, sym: Symbol, depth: int = 0) -> None:
        self.emit(depth, f"{sym.name} = {value_str(sym)} ({how_set(sym)}), needs n - "
                         f"{type_str(sym)} at {loc(sym)}")
        if expr_value(sym.rev_dep):
            self.emit(depth + 1, "it is forced on by 'select' - every active selector has to go:")
            for s in _true_selectors(sym):
                self.emit(depth + 2, f"{s.name} = {s.str_value} ({how_set(s)}) at {loc(s)}")
        elif not has_prompt(sym):
            self.emit(depth + 1, "it has no prompt; its value comes from a default whose "
                                 "condition is true:")
            for val, cond in sym.defaults:
                if expr_value(cond):
                    self.emit(depth + 2, f"default {expr_str(val)} if {expr_str(cond)}")
                    break
        elif sym.choice is not None:
            self.emit(depth + 1, f"it is the selected member of a choice ({loc(sym.choice)}); "
                                 f"select another member instead.")
        else:
            self.emit(depth + 1, f"nothing forces it - set {sym.name}=n directly.")

    def _choice(self, sym: Symbol, depth: int) -> None:
        ch = sym.choice
        sel = ch.selection
        self.emit(depth, f"{sym.name} is a member of a choice ({loc(ch)}); currently selected: "
                         f"{sel.name if sel else 'none'}.")
        if ch.visibility == 0:
            self.emit(depth, "the choice itself is hidden:")
            for n in ch.nodes:
                if n.prompt:
                    self._unmet(n.prompt[1], 2, depth + 1)
        for n in sym.nodes:
            if n.prompt and expr_value(n.prompt[1]) < 2:
                self.emit(depth, f"this member's prompt needs {expr_str(n.prompt[1])}:")
                self._unmet(n.prompt[1], 2, depth + 1)

    def _sel_loc(self, term: Any, target: Symbol) -> str:
        for s in _syms_in(term):
            if any(t is target for t, _ in s.selects):
                return f"  [{s.name} at {loc(s)}]"
        return ""

    def _unmet(self, e: Any, need: int, depth: int) -> None:
        """Report the parts of expression e that keep it below need."""
        if expr_value(e) >= need:
            return
        if isinstance(e, Symbol):
            if e.is_constant:
                self.emit(depth, f"the constant {e.name} - this can never be satisfied")
            else:
                self.why_not(e, need, depth)
            return
        if isinstance(e, Choice):
            self.emit(depth, f"choice at {loc(e)} is {TRI_STR[e.tri_value]}")
            return
        op = e[0]
        if op == AND:
            for part in split_expr(e, AND):
                self._unmet(part, need, depth)
        elif op == OR:
            parts = split_expr(e, OR)
            self.emit(depth, f"needs any one of ({len(parts)} options, all currently too low):")
            for part in parts[:6]:
                self._unmet(part, need, depth + 1)
        elif op == NOT:
            inner = e[1]
            if isinstance(inner, Symbol):
                self.emit(depth, f"requires {inner.name} to be off (! {inner.name}):")
                self.why_not_off(inner, depth + 1)
            else:
                self.emit(depth, f"requires !({expr_str(inner)}), but {expr_str(inner)} = "
                                 f"{TRI_STR[expr_value(inner)]}")
        elif op in CMP_OPS:
            a, b = e[1], e[2]
            if a.is_constant and b.is_constant:
                # Both sides were fixed when the tree was parsed - almost always
                # a $(VAR) such as $(ARCH) expanded from the environment.
                self.emit(depth, f"requires {expr_str(e)}, which compares two constants and is "
                                 f"always false here. One side was probably expanded from an "
                                 f"environment variable (like $(ARCH)) when the tree was loaded: "
                                 f"no symbol change can fix this - reload with a different env.")
            else:
                self.emit(depth, f"requires {expr_str(e)}, but {a.name} = {value_str(a)}"
                                 + ("" if b.is_constant else f" and {b.name} = {value_str(b)}"))


# -- planning --------------------------------------------------------------

class Planner:
    """Search for small sets of user assignments that reach a goal."""

    def __init__(self, kconf: kconfiglib.Kconfig, budget: int = 20000):
        self.kconf = kconf
        # Real trees are huge (Linux: ~19k symbols, selectors with dozens of
        # alternatives), so the search is memoized and bounded.
        self.memo: dict[tuple[str, int, str], list[Plan]] = {}
        self.cuts = 0          # times a cycle or depth limit truncated a result
        self.budget = budget   # symbol expansions left
        self.exhausted = False

    def for_sym(self, sym: Symbol, need: int, depth: int = 0,
                stack: frozenset[str] = frozenset()) -> list[Plan]:
        if need == 1 and sym.orig_type == kconfiglib.BOOL:
            need = 2  # kconfig promotes m to y for bool symbols
        if sym.tri_value >= need:
            return [{}]
        if sym.is_constant or not sym.nodes:
            return []
        if depth > MAX_DEPTH or sym.name in stack:
            self.cuts += 1
            return []
        key = (sym.name, need, "on")
        if key in self.memo:
            return self.memo[key]
        if self.budget <= 0:
            self.exhausted = True
            return []
        self.budget -= 1
        cuts_before = self.cuts
        result = self._for_sym(sym, need, depth, stack | {sym.name})
        # A result truncated by a cycle depends on the path taken to get
        # here, so only path-independent results are cached.
        if self.cuts == cuts_before:
            self.memo[key] = result
        return result

    def _for_sym(self, sym: Symbol, need: int, depth: int, stack: frozenset[str]) -> list[Plan]:
        plans: list[Plan] = []
        want = "m" if need == 1 and sym.orig_type == kconfiglib.TRISTATE else "y"
        if sym.choice is not None:
            ch = sym.choice
            for n in sym.nodes:
                if n.prompt:
                    for p in self.for_expr(n.prompt[1], 2, depth + 1, stack):
                        plans.extend(_merge([p], [{sym.name: "y"}]))
            for n in ch.nodes:
                if n.prompt and expr_value(n.prompt[1]) < 2:
                    plans = [m for p in plans for m in _merge(
                        [p], self.for_expr(n.prompt[1], 2, depth + 1, stack))]
        else:
            for n in sym.nodes:
                if n.prompt:
                    for p in self.for_expr(n.prompt[1], need, depth + 1, stack):
                        plans.extend(_merge([p], [{sym.name: want}]))
            if want == "m":
                plans = [m for p in plans for m in _merge(
                    [p], self.for_sym(self.kconf.modules, 2, depth + 1, stack))]
            # Selects and defaults are routes around the prompt. Only explore
            # them when the prompt route found nothing: they fan out enormously
            # on real trees, and forcing a symbol past its dependencies via
            # select is the last resort anyway.
            if not plans:
                for term in split_expr(sym.rev_dep, OR):
                    if term is not self.kconf.n:
                        plans.extend(self.for_expr(term, need, depth + 1, stack))
            if not plans and (not has_prompt(sym) or sym.user_value is None):
                for val, cond in sym.defaults:
                    goal = (AND, (AND, cond, val), sym.direct_dep)
                    plans.extend(self.for_expr(goal, need, depth + 1, stack))
        return _prune(plans)

    def for_expr(self, e: Any, need: int, depth: int, stack: frozenset[str]) -> list[Plan]:
        if expr_value(e) >= need:
            return [{}]
        if isinstance(e, Symbol):
            return self.for_sym(e, need, depth, stack)
        if isinstance(e, Choice):
            return []
        op = e[0]
        if op == AND:
            acc: list[Plan] = [{}]
            for part in split_expr(e, AND):
                acc = _prune([m for a in acc for m in _merge(
                    [a], self.for_expr(part, need, depth, stack))])
                if not acc:
                    return []
            return acc
        if op == OR:
            out: list[Plan] = []
            for part in split_expr(e, OR):
                out.extend(self.for_expr(part, need, depth, stack))
            return _prune(out)
        if op == NOT:
            return self.off_expr(e[1], depth, stack)
        if op in CMP_OPS:
            a, b = e[1], e[2]
            if op == kconfiglib.EQUAL and b.is_constant and b.name in TRI and is_bool_like(a):
                if b.name == "n":
                    return self.off_expr(a, depth, stack)
                return self.for_sym(a, TRI[b.name], depth, stack)
            if op == kconfiglib.UNEQUAL and b is self.kconf.n:
                return self.for_sym(a, 1, depth, stack)
        return []

    def off_expr(self, e: Any, depth: int, stack: frozenset[str]) -> list[Plan]:
        """Plans that drive e to n."""
        if expr_value(e) == 0:
            return [{}]
        if depth > MAX_DEPTH:
            return []
        if isinstance(e, Symbol):
            return self.off_sym(e, depth, stack)
        if isinstance(e, Choice):
            return []
        op = e[0]
        if op == AND:  # any one conjunct off
            out: list[Plan] = []
            for part in split_expr(e, AND):
                out.extend(self.off_expr(part, depth, stack))
            return _prune(out)
        if op == OR:  # every disjunct off
            acc: list[Plan] = [{}]
            for part in split_expr(e, OR):
                acc = _prune([m for a in acc for m in _merge([a], self.off_expr(part, depth, stack))])
                if not acc:
                    return []
            return acc
        if op == NOT:
            return self.for_expr(e[1], 2, depth, stack)
        return []

    def off_sym(self, sym: Symbol, depth: int, stack: frozenset[str]) -> list[Plan]:
        if sym.tri_value == 0:
            return [{}]
        if sym.is_constant:
            return []
        if sym.name in stack or depth > MAX_DEPTH:
            self.cuts += 1
            return []
        key = (sym.name, 0, "off")
        if key in self.memo:
            return self.memo[key]
        if self.budget <= 0:
            self.exhausted = True
            return []
        self.budget -= 1
        cuts_before = self.cuts
        result = self._off_sym(sym, depth, stack | {sym.name})
        if self.cuts == cuts_before:
            self.memo[key] = result
        return result

    def _off_sym(self, sym: Symbol, depth: int, stack: frozenset[str]) -> list[Plan]:
        base: list[Plan] = [{}]
        if expr_value(sym.rev_dep):
            for term in split_expr(sym.rev_dep, OR):
                if expr_value(term):
                    base = _prune([m for a in base for m in _merge(
                        [a], self.off_expr(term, depth + 1, stack))])
        if not base:
            return []
        if has_prompt(sym) and sym.choice is None:
            return _prune([m for a in base for m in _merge([a], [{sym.name: "n"}])])
        # Promptless: falsify whichever default is currently active.
        out: list[Plan] = []
        for _val, cond in sym.defaults:
            if expr_value(cond):
                out.extend(m for a in base for m in _merge([a], self.off_expr(cond, depth + 1, stack)))
                break
        return _prune(out) if out else base


def _merge(left: Iterable[Plan], right: Iterable[Plan]) -> list[Plan]:
    out = []
    right = list(right)
    for a in left:
        for b in right:
            if all(a[k] == v for k, v in b.items() if k in a):
                out.append({**a, **b})
    return out


def _prune(plans: list[Plan]) -> list[Plan]:
    seen: set[frozenset[tuple[str, str]]] = set()
    uniq = []
    for p in sorted(plans, key=len):
        key = frozenset(p.items())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq[:MAX_PLANS]


# -- applying and snapshotting -------------------------------------------------

def snapshot(kconf: kconfiglib.Kconfig) -> tuple[dict[str, Any], list[tuple[Choice, Symbol]]]:
    syms = {s.name: s.user_value for s in kconf.unique_defined_syms if s.user_value is not None}
    choices = [(c, c.user_selection) for c in kconf.unique_choices if c.user_selection is not None]
    return syms, choices


def restore(kconf: kconfiglib.Kconfig, snap: tuple[dict[str, Any], list[tuple[Choice, Symbol]]]) -> None:
    syms, choices = snap
    kconf.unset_values()
    for name, val in syms.items():
        s = kconf.syms[name]
        s.set_value(val if isinstance(val, str) else TRI_STR[val] if is_bool_like(s) else val)
    for _c, sel in choices:
        sel.set_value(2)


def values(kconf: kconfiglib.Kconfig) -> dict[str, str]:
    return {s.name: s.str_value for s in kconf.unique_defined_syms}


def apply_plan(kconf: kconfiglib.Kconfig, plan: Plan) -> None:
    """Apply assignments, repeating until stable: setting a symbol can fail
    until the symbols that make its prompt visible have been set."""
    for _ in range(len(plan) + 1):
        changed = False
        for name, val in plan.items():
            s = kconf.syms[name]
            if s.str_value != val:
                s.set_value(val)
                changed = changed or s.str_value == val
        if not changed:
            break


def verify(kconf: kconfiglib.Kconfig, sym: Symbol, need: int, plans: list[Plan]) -> tuple[Plan, dict[str, tuple[str, str]]] | None:
    """Return the first plan that really reaches the goal, with its side effects."""
    snap = snapshot(kconf)
    before = values(kconf)
    try:
        for plan in plans[:MAX_VERIFY]:
            apply_plan(kconf, plan)
            ok = sym.tri_value == need if need == 0 else sym.tri_value >= need
            if ok:
                after = values(kconf)
                effects = {k: (before.get(k, ""), v) for k, v in after.items()
                           if before.get(k) != v and k not in plan and k != sym.name}
                return plan, effects
            restore(kconf, snap)
    finally:
        restore(kconf, snap)
    return None


def plan_for(kconf: kconfiglib.Kconfig, sym: Symbol, need: int) -> tuple[list[Plan], bool]:
    """Candidate plans, and whether the search budget ran out."""
    p = Planner(kconf)
    plans = p.off_sym(sym, 0, frozenset()) if need == 0 else p.for_sym(sym, need)
    return plans, p.exhausted


__all__ = ["Explainer", "Planner", "verify", "plan_for", "apply_plan", "snapshot", "restore",
           "values", "loc", "how_set", "type_str", "has_prompt", "value_str", "TRI", "TRI_STR"]

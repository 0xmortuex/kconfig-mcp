# Changelog

## 0.1.0 - 2026-10-08

First release.

- `kconfig_why_not`: explains every unmet `depends on`, `visible if`, `if` block,
  choice, select and default condition behind a symbol, with file:line, recursively.
  It then searches for a minimal set of assignments and **verifies** it by applying it to
  the tree, reporting side effects. Only verified fixes are presented as fixes.
- `kconfig_load`, `kconfig_symbol`, `kconfig_what_selects`, `kconfig_search`,
  `kconfig_set` (reports consequences and refused assignments), `kconfig_diff`
  (full .config or fragment), `kconfig_save` (full or minimal), sessions.
- Parses current Linux despite Kconfiglib 14.1.0 predating its syntax: bare
  `modules`, `transitional`, and `depends on X if Y`, the last lowered with a port of
  scripts/kconfig/expr.c's `expr_trans_compare`.
- Names undefined environment variables (e.g. SRCARCH) in load errors.
- `shell="dry"` loads trees without running `$(shell,...)` probes, so no compiler
  is needed and it works on Windows.

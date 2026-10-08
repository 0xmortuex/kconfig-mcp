# Backlog

Real, finishable improvements - pick ONE, ship it end-to-end with tests.

## Features
- [ ] `transitional` symbols: honour the old .config value the way scripts/kconfig does (today they are always n).
- [ ] Zephyr integration test (needs ZEPHYR_BASE + board/SoC Kconfig env; west not required for parsing).
- [ ] `kconfig_why_not` for int/hex/string symbols: report range and visibility blockers.
- [ ] Rank verified fixes by side-effect count, and offer the top 2 verified alternatives, not just one.
- [ ] Cache parsed trees (pickle keyed on Kconfig mtimes + env) - a cold Linux parse is 10-40 s.
- [ ] `kconfig_explain_config`: why does a loaded .config have CONFIG_X=y (user, select chain, default)?

## Quality
- [ ] Explanation dedup: long OR-chains in real trees repeat the same subtrees ("see above" helps but not across branches).
- [ ] Integration test for U-Boot and Buildroot trees.

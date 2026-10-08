# kconfig-mcp

**"Why can't I enable CONFIG_X?", answered with file:line and a fix that's been checked.**

<!-- mcp-name: io.github.0xmortuex/kconfig-mcp -->

kconfig-mcp is an [MCP](https://modelcontextprotocol.io) server that lets an AI agent
reason about Kconfig, the configuration system used by the Linux kernel, Zephyr,
U-Boot, Buildroot and others.

Everyone who has configured a kernel has hit this: the option you want isn't in
menuconfig, or it's there but greyed out, and the reason is five `depends on` hops
away in a different directory. kconfig-mcp walks that chain for you. Then it searches
for the smallest set of changes that would unlock the option, and **applies them to
the real tree to verify** they work before it shows them to you.

## What it looks like

This is real output on the current Linux tree (18,913 symbols) for x86:

```
> kconfig_why_not X86_X2APIC
X86_X2APIC = n (default), needs y - bool at arch/x86/Kconfig:463
  its prompt (arch/x86/Kconfig:463) is visible only if
  X86_LOCAL_APIC && X86_64 && (IRQ_REMAP || HYPERVISOR_GUEST), which is n:
    needs any one of (2 options, all currently too low):
      IRQ_REMAP = n (default), needs y - bool at drivers/iommu/Kconfig:201
        its prompt is visible only if X86_64 && X86_IO_APIC && PCI_MSI && ACPI && IOMMU_SUPPORT:
          PCI_MSI = n (default), needs y - bool at drivers/pci/Kconfig:44
            its prompt is visible only if PCI, which is n:
              PCI = n (default) - set PCI=y directly.
      HYPERVISOR_GUEST = n (default), needs y - bool at arch/x86/Kconfig:787
        its prompt is visible - set HYPERVISOR_GUEST=y directly.

Verified fix (2 change(s)): HYPERVISOR_GUEST=y, X86_X2APIC=y
  HYPERVISOR_GUEST: arch/x86/Kconfig:787
  X86_X2APIC: arch/x86/Kconfig:463
Unverified alternatives: PCI=y, PCI_MSI=y, IRQ_REMAP=y, X86_X2APIC=y

> kconfig_set HYPERVISOR_GUEST y
CONFIG_HYPERVISOR_GUEST: 'n' -> 'y'.
1 other symbol(s) changed as a consequence: X86_X2APIC n->y
```

The same question on a 32-bit build (`ARCH=i386`) ends somewhere no symbol change can
reach, and the tool says so instead of inventing a fix:

```
64BIT = n (default), needs y - bool at arch/x86/Kconfig:3
  requires "i386" = "x86", which compares two constants and is always false here.
  One side was probably expanded from an environment variable (like $(ARCH)) when
  the tree was loaded: no symbol change can fix this - reload with a different env.
No verified fix: every route ends in something no assignment can change.
```

## Why it's built this way

- **Fixes are verified, not guessed.** Kconfig is full of edge cases: tristate `m`
  needs `MODULES`, bools promote `m` to `y`, choices deselect their other members,
  `select` overrides `depends on`, and `default y` turns things on silently. So every
  candidate plan is applied to the loaded tree and checked. Only plans that really
  reach the goal are shown as fixes, together with their side effects (what else
  `select`/`imply`/defaults switch on or off).
- **It parses current Linux.** It's built on Kconfiglib, which hasn't had a release
  since 2020 and can't parse Linux's newer `modules`, `transitional` and
  `depends on X if Y` syntax. kconfig-mcp rewrites those lines as files are read,
  keeping every line number exact. `depends on X if Y` is lowered with a line-by-line
  port of `expr_trans_compare` from `scripts/kconfig/expr.c`, so it behaves exactly
  as kconfig does, including how kconfig treats `!S` when S is `m`.
- **Environment problems are named.** Linux sources `arch/$(SRCARCH)/Kconfig`, and an
  unset variable silently becomes `""`. kconfig-mcp tracks undefined variables and
  names them in the error (`undefined variable(s): SRCARCH (the arch/ directory to
  use, ...)`) instead of leaving you with `arch//Kconfig not found`.

## Install

```bash
pip install kconfig-mcp
claude mcp add kconfig -- kconfig-mcp
```

Python 3.10+. You don't need a kernel build environment. A source tree with its
Kconfig files is enough, even a sparse checkout:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/torvalds/linux.git
cd linux && git sparse-checkout set --no-cone '/Kconfig' '**/Kconfig*' '/arch/*/configs/*'
```

## Loading trees

| Tree | `kconfig_load` arguments |
|------|---------------------------|
| Linux | `kconfig_path="linux/Kconfig"`, `env={"SRCARCH": "x86", "ARCH": "x86"}` (or `arm64`/`arm64`, `riscv`/`riscv`, ...). Add `config_path=".config"` for an existing config. |
| Linux, no compiler / Windows | the same plus `shell="dry"` |
| Any other tree | the top-level `Kconfig` plus whatever variables it expands. A load error names each missing one. |

`shell` controls the tree's `$(shell,...)` calls, which is how Linux probes the
compiler:

- **`"run"`** (default) runs them with a POSIX `sh`, as `make menuconfig` would.
  Compiler-dependent symbols then reflect your real toolchain.
- **`"dry"`** runs nothing. Linux's probe idioms get fixed answers: feature probes
  read as absent, and the toolchain reports as GCC 13.2. `$(error-if)` checks become
  warnings. Results then carry a note that compiler-dependent symbols such as
  `CC_HAS_*` read as absent.

## Tools

| Tool | What it does |
|------|--------------|
| `kconfig_load` | Parse a tree (plus optional `.config`) into a named session. Reports undefined variables and warnings. |
| `kconfig_why_not` | The unmet conditions behind `symbol` not being `y`/`m`/`n`, recursively with file:line, plus a verified minimal fix and its side effects. |
| `kconfig_symbol` | Type, prompt, value and *why* it has that value, assignable values, dependencies, defaults, select/imply in both directions, help text. |
| `kconfig_what_selects` | Every symbol that selects or implies this one, with current values and which are active. |
| `kconfig_search` | Regex search over names, prompts and help, with name matches first. |
| `kconfig_set` | Assign a value as menuconfig would, and report every other symbol that changed. A refused assignment says so. |
| `kconfig_diff` | Compare with another `.config`, or with a defconfig or fragment using `fragment=True`. |
| `kconfig_save` | Write `.config`, or a minimal defconfig with `minimal=True`. |
| `kconfig_sessions` / `kconfig_unload` | Manage loaded trees. |

Symbol names work with or without `CONFIG_`. A misspelt name gets "Did you mean: ...".

## Limitations

- **`transitional` symbols are always `n`.** Real kconfig reads them from an old
  `.config` to migrate a value during a rename. Kconfiglib has no equivalent.
- **The fix search is bounded.** Selects and defaults are only explored when a
  symbol can't be reached through its own prompt, and the search has an expansion
  budget. If no verified fix is found, the result says whether the budget ran out
  or every route is genuinely blocked.
- **`why_not` covers bool and tristate symbols.** For int/hex/string symbols, use
  `kconfig_symbol` and `kconfig_set`.
- **Dry mode answers compiler probes generically** (see above), so trust
  `CC_HAS_*`-style symbols only with `shell="run"` on a real toolchain.
- It depends on Kconfiglib 14.1.0, pinned because `compat.py` hooks two of its
  internal methods. CI parses the latest Linux tree every week, so the next new
  keyword shows up as a failing build rather than a silent wrong answer.

## Tests

```bash
pip install ".[test]" "ruff==0.16.0" "mypy==2.3.0"
ruff check src tests && mypy --strict src
pytest tests                                   # unit + stdio tests, fixture trees
LINUX_SRC=/path/to/linux pytest tests/test_linux_integration.py
```

The fixture tree in `tests/fixtures/tree` exercises every construct the explainer
handles: depends on, select, imply, choice, `visible if`, menuconfig/if blocks,
defaults, tristate and modules, and an arch directory chosen by `$(SRCARCH)`. The
Linux tests assert real facts, such as x2APIC's dependencies, the i386 dead end, and
the x86_64 defconfig already enabling it. CI runs them against a sparse checkout of
the current tree.

## License

MIT. Kconfiglib is ISC-licensed.

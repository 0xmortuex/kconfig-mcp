"""Against a real Linux tree. Set LINUX_SRC to a checkout (a sparse one with
just the Kconfig files is enough - see .github/workflows/ci.yml). Parsing
Linux takes 10-40 s per load, so loads are shared per module.
"""

import os

import pytest

from kconfig_mcp import server as S

LINUX = os.environ.get("LINUX_SRC")
pytestmark = pytest.mark.skipif(not LINUX, reason="set LINUX_SRC to a Linux checkout")


@pytest.fixture(scope="module")
def linux():
    S._sessions.clear()
    root = os.path.join(LINUX, "Kconfig")
    S.kconfig_load("x86", root, env={"SRCARCH": "x86", "ARCH": "x86"}, shell="dry")
    S.kconfig_load("i386", root, env={"SRCARCH": "x86", "ARCH": "i386"}, shell="dry")
    S.kconfig_load("defconfig", root, env={"SRCARCH": "x86", "ARCH": "x86"}, shell="dry",
                   config_path=os.path.join(LINUX, "arch", "x86", "configs", "x86_64_defconfig"))
    yield
    S._sessions.clear()


def test_parses_current_linux_with_new_syntax(linux):
    k = S._sessions["x86"].kconf
    assert len(k.unique_defined_syms) > 15000
    # `modules` keyword (kernel/module/Kconfig) became the modules symbol
    assert k.modules.name == "MODULES"
    # `depends on USB if !USB_GADGET` lowered the way menu.c does it
    import kconfiglib
    dep = kconfiglib.expr_str(k.syms["USB_CDNS_SUPPORT"].direct_dep)
    assert "USB_GADGET != n" in dep and "USB != n" in dep


def test_x2apic_on_x86_64_has_a_verified_fix(linux):
    out = S.kconfig_why_not("x86", "CONFIG_X86_X2APIC")
    # arch/x86/Kconfig: depends on X86_LOCAL_APIC && X86_64 && (IRQ_REMAP || HYPERVISOR_GUEST)
    assert "arch/x86/Kconfig" in out
    assert "IRQ_REMAP" in out and "HYPERVISOR_GUEST" in out
    assert "Verified fix" in out and "HYPERVISOR_GUEST=y" in out


def test_x2apic_on_i386_is_blocked_by_the_environment(linux):
    out = S.kconfig_why_not("i386", "X86_X2APIC")
    # 64BIT's prompt is `bool "64-bit kernel" if "$(ARCH)" = "x86"`
    assert "64BIT" in out and "compares two constants" in out
    assert "No verified fix" in out


def test_defconfig_already_has_x2apic(linux):
    # x86_64_defconfig sets CONFIG_HYPERVISOR_GUEST=y, and X86_X2APIC defaults to y.
    assert "already y" in S.kconfig_why_not("defconfig", "X86_X2APIC")


def test_symbol_and_search_on_linux(linux):
    out = S.kconfig_symbol("x86", "X86_X2APIC")
    assert "x2APIC interrupt controller architecture support" in out
    assert "X86_X2APIC" in S.kconfig_search("x86", "^X86_X2APIC$")

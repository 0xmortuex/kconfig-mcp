"""The MCP tools against the fixture tree. Every Kconfig construct the
explainer and planner handle appears in tests/fixtures/tree/Kconfig."""

import os

import pytest

from kconfig_mcp import server as S

HERE = os.path.dirname(__file__)
TREE = os.path.join(HERE, "fixtures", "tree", "Kconfig")
OTHER = os.path.join(HERE, "fixtures", "other.config")


@pytest.fixture
def x86():
    S._sessions.clear()
    S.kconfig_load("t", TREE, env={"SRCARCH": "x86"})
    yield "t"
    S._sessions.clear()


@pytest.fixture
def arm():
    S._sessions.clear()
    S.kconfig_load("a", TREE, env={"SRCARCH": "arm"})
    yield "a"
    S._sessions.clear()


def test_load_summary(x86):
    out = S.kconfig_sessions()
    assert "t:" in out and "SRCARCH" in out


def test_duplicate_session_name_rejected(x86):
    with pytest.raises(ValueError, match="already loaded"):
        S.kconfig_load("t", TREE, env={"SRCARCH": "x86"})


def test_why_not_depends_on_and_if_block(x86):
    out = S.kconfig_why_not(x86, "CONFIG_WIFI_DRIVER")
    assert "visible only if PCI && WIRELESS" in out
    assert "Verified fix (3 change(s)): PCI=y, WIRELESS=y, WIFI_DRIVER=y" in out
    # select and imply side effects are reported, with their direction
    assert "FIRMWARE_LOADER n->y" in out and "LEDS n->y" in out


def test_why_not_unreachable_on_other_arch(arm):
    out = S.kconfig_why_not(arm, "WIFI_DRIVER")
    assert "HAVE_PCI has no prompt" in out
    assert "nothing in this tree selects it" in out
    assert "No verified fix" in out


def test_why_not_negated_dependency(x86):
    out = S.kconfig_why_not(x86, "DEBUG_VM")
    assert "requires SMALL_BUILD to be off" in out
    assert "DEBUG_KERNEL=y" in out and "SMALL_BUILD=n" in out


def test_why_not_choice_member(x86):
    out = S.kconfig_why_not(x86, "SCHED_FAIR")
    assert "member of a choice" in out and "currently selected: SCHED_SIMPLE" in out
    assert "SMP=y, SCHED_FAIR=y" in out and "SCHED_SIMPLE y->n" in out


def test_why_not_visible_if_menu(x86):
    out = S.kconfig_why_not(x86, "HIDDEN_KNOB")
    assert "visible only if EXPERT" in out and "EXPERT=y" in out


def test_why_not_promptless_select_only(x86):
    out = S.kconfig_why_not(x86, "FIRMWARE_LOADER")
    assert "has no prompt" in out and "WIFI_DRIVER" in out
    assert "Verified fix" in out


def test_why_not_tristate_m_needs_modules(x86):
    out = S.kconfig_why_not(x86, "WIFI_DRIVER", "m")
    assert "m requires module support" in out
    assert "WIFI_DRIVER=m" in out and "MODULES=y" in out
    # bool dependencies of an m request are explained as y, never "set PCI=m"
    assert "PCI=m" not in out


def test_why_not_off_and_already_there(x86):
    assert "nothing forces it" in S.kconfig_why_not(x86, "NET", "n")
    assert "already y" in S.kconfig_why_not(x86, "NET", "y")


def test_why_not_rejects_bad_values(x86):
    with pytest.raises(ValueError, match="bool, so it can't be m"):
        S.kconfig_why_not(x86, "PCI", "m")
    with pytest.raises(ValueError, match="int symbol"):
        S.kconfig_why_not(x86, "LOG_LEVEL")


def test_why_not_does_not_change_the_config(x86):
    S.kconfig_why_not(x86, "WIFI_DRIVER")
    k = S._sessions[x86].kconf
    assert k.syms["PCI"].str_value == "n" and k.syms["WIFI_DRIVER"].str_value == "n"


def test_symbol_details(x86):
    out = S.kconfig_symbol(x86, "WIFI_DRIVER")
    assert "type: tristate" in out and 'Kconfig:25 "Wi-Fi driver"' in out
    assert "selects: FIRMWARE_LOADER" in out and "implies: LEDS" in out
    assert "A Wi-Fi driver for a PCI card." in out


def test_unknown_symbol_suggests(x86):
    with pytest.raises(KeyError, match="Did you mean: WIFI_DRIVER"):
        S.kconfig_symbol(x86, "WIFI_DRVER")


def test_what_selects(x86):
    out = S.kconfig_what_selects(x86, "FIRMWARE_LOADER")
    assert "selected by (1):" in out and "WIFI_DRIVER" in out
    assert "implied by (1):" in S.kconfig_what_selects(x86, "LEDS")


def test_search_ranks_names_then_prompts_then_help(x86):
    out = S.kconfig_search(x86, "net")
    lines = out.splitlines()[1:]
    assert lines[0].startswith("NET") and any("NO_NET" in ln for ln in lines)
    assert "No symbols match" in S.kconfig_search(x86, "zzz")


def test_set_reports_consequences_and_refusals(x86):
    out = S.kconfig_set(x86, "HIDDEN_KNOB", "y")
    assert "did NOT become 'y'" in out and "remembered" in out
    out = S.kconfig_set(x86, "EXPERT", "y")
    assert "'n' -> 'y'" in out and "HIDDEN_KNOB n->y" in out


def test_diff_full_and_fragment(x86):
    full = S.kconfig_diff(x86, OTHER)
    assert "X86: y here, n in other.config" in full   # missing == n in a full .config
    frag = S.kconfig_diff(x86, OTHER, fragment=True)
    assert "X86" not in frag
    assert "LOG_LEVEL: 4 here, 6 in other.config" in frag
    assert frag.startswith("4 difference(s):")


def test_save_roundtrip(x86, tmp_path):
    S.kconfig_set(x86, "DEBUG_KERNEL", "y")
    path = tmp_path / ".config"
    assert "saved" in S.kconfig_save(x86, str(path)).lower() or path.exists()
    S.kconfig_load("r", TREE, config_path=str(path), env={"SRCARCH": "x86"})
    assert S._sessions["r"].kconf.syms["DEBUG_KERNEL"].str_value == "y"
    mini = tmp_path / "defconfig"
    S.kconfig_save(x86, str(mini), minimal=True)
    text = mini.read_text()
    assert "CONFIG_DEBUG_KERNEL=y" in text and "CONFIG_NET" not in text  # NET is default y


def test_unknown_session(x86):
    with pytest.raises(KeyError, match="call kconfig_load first"):
        S.kconfig_symbol("nope", "NET")

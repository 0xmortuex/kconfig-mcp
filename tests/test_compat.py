"""The syntax shims that let Kconfiglib 14.1.0 parse current Linux."""

import pytest

from kconfig_mcp.compat import UnsupportedSyntax, _Parser, rewrite_line, trans_compare_no


def test_bare_modules_becomes_option_modules():
    assert rewrite_line("\tmodules\n", "K", 1) == "\toption modules\n"


def test_transitional_is_dropped_but_keeps_the_line():
    assert rewrite_line("\ttransitional\n", "K", 1) == "\n"


def test_help_text_is_never_rewritten():
    # Help is indented with tab + spaces; only tab-only property lines match.
    for line in ("\t  modules\n", "\t  transitional\n", "\t  depends on A if B\n"):
        assert rewrite_line(line, "K", 1) == line


def test_if_inside_a_comment_is_not_a_condition():
    # Real line from drivers/net/wireless/silabs/wfx/Kconfig:5
    line = "\tdepends on MMC || !MMC # do not allow WFX=y if MMC=m\n"
    assert rewrite_line(line, "K", 5) == line


def _lower(cond: str, equal: bool = True) -> str:
    return trans_compare_no(_Parser(cond).parse(), equal)


@pytest.mark.parametrize("cond, expected", [
    # The examples documented above expr_trans_compare in scripts/kconfig/expr.c
    # (type == E_UNEQUAL, sym == &symbol_no):
    ("A", "(A != n)"),
    ("!A", "(A = n)"),
    ("A && B", "!((A = n) || (B = n))"),
    ("A || B", "!((A = n) && (B = n))"),
    ("A && (B || C)", "!((A = n) || ((B = n) && (C = n)))"),
])
def test_trans_compare_matches_expr_c_documented_examples(cond, expected):
    assert _lower(cond, equal=False) == expected


def test_not_symbol_equal_n_is_symbol_unequal_n_like_kconfig():
    # kconfig lowers (!S == n) to (S != n), not (S = y): they differ for S=m.
    assert _lower("!USB_GADGET") == "(USB_GADGET != n)"


def test_depends_on_if_lowering_full_line():
    # Real lines from drivers/usb/cdns3/Kconfig and drivers/hv/Kconfig
    assert rewrite_line("\tdepends on USB if !USB_GADGET\n", "K", 5) == \
        "\tdepends on (USB) || (USB_GADGET != n)\n"
    assert rewrite_line("\tdepends on HYPERV_VMBUS if HYPERV_VMBUS\n", "K", 77) == \
        "\tdepends on (HYPERV_VMBUS) || (HYPERV_VMBUS = n)\n"


def test_comparison_condition():
    assert _lower("MMC=m") == "!(MMC = m)"
    assert _lower("MMC=m", equal=False) == "(MMC = m)"


def test_unparseable_condition_is_an_error_not_a_guess():
    with pytest.raises(UnsupportedSyntax, match="K:9"):
        rewrite_line("\tdepends on A if (B\n", "K", 9)


def test_line_count_is_preserved_for_every_rewrite():
    for line in ("\tmodules\n", "\ttransitional\n", "\tdepends on A if B\n"):
        assert rewrite_line(line, "K", 1).count("\n") == 1

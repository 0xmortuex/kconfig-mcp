import os
import shutil

import pytest

from kconfig_mcp import loader
from kconfig_mcp.loader import LoadError, _dry_shell, load

HERE = os.path.dirname(__file__)
TREE = os.path.join(HERE, "fixtures", "tree", "Kconfig")
SHELL_TREE = os.path.join(HERE, "fixtures", "shelltree", "Kconfig")


def test_missing_env_var_is_named():
    with pytest.raises(LoadError) as ei:
        load(TREE, None, {})
    msg = str(ei.value)
    assert "arch//Kconfig" in msg
    assert "undefined variable(s): SRCARCH" in msg and "arch/ directory" in msg


def test_env_is_scoped_to_the_load():
    os.environ.pop("SRCARCH", None)
    r = load(TREE, None, {"SRCARCH": "arm"})
    assert "SRCARCH" not in os.environ
    assert r.kconf.syms["ARM"].str_value == "y"
    assert "X86" not in r.kconf.syms  # never even referenced on arm


def test_missing_files_are_explicit(tmp_path):
    with pytest.raises(FileNotFoundError, match="Kconfig file not found"):
        load(str(tmp_path / "nope"), None, {})
    with pytest.raises(FileNotFoundError, match=".config file not found"):
        load(TREE, str(tmp_path / "nope.config"), {"SRCARCH": "x86"})


def test_bad_shell_mode():
    with pytest.raises(ValueError, match="shell must be"):
        load(TREE, None, {"SRCARCH": "x86"}, shell="maybe")


@pytest.mark.parametrize("cmd, out", [
    ('{ gcc -m64; } >/dev/null 2>&1 && echo "y" || echo "n"', "n"),   # if-success -> absent
    ("set -- GCC 130200 && echo $1", "GCC"),
    ("set -- GCC 130200 && echo $2", "130200"),
    ("expr 130200 / 10000", "13"),
    ("/src/scripts/cc-version.sh gcc", "GCC 130200"),
    ("/src/scripts/rustc-version.sh rustc", "0"),
    ("anything else", ""),
])
def test_dry_shell_idioms(cmd, out):
    assert _dry_shell(cmd) == out


def test_dry_mode_loads_shell_tree_without_running_anything():
    r = load(SHELL_TREE, None, {}, shell="dry")
    k = r.kconf
    assert r.shell_cmds == 3
    assert k.syms["HAS_TRUE"].str_value == "n"        # probe assumed to fail
    assert k.syms["COMPILER_MAJOR"].str_value == "13"
    assert k.syms["GREETING"].str_value == ""


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_run_mode_executes_with_posix_sh():
    k = load(SHELL_TREE, None, {}, shell="run").kconf
    assert k.syms["GREETING"].str_value == "hello"
    assert k.syms["HAS_TRUE"].str_value == "y"
    assert k.syms["COMPILER_MAJOR"].str_value == "13"


def test_run_mode_without_sh_says_what_to_do(monkeypatch):
    monkeypatch.setattr(loader.shutil, "which", lambda name: None)
    with pytest.raises(LoadError, match='shell="dry"'):
        load(SHELL_TREE, None, {}, shell="run")

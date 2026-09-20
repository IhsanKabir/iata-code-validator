"""The PyInstaller spec must know about every module the GUI defers.

The GUI imports its heavier modules inside the handler that needs them, so
startup does not pay for tabs nobody opens. PyInstaller's static analysis
usually follows a function-level `from . import X`, but the spec already had
to name several by hand, with the comment that a missed one "loses the whole
tab silently" -- the failure shows up only in the packaged .exe, never in the
tests, because the tests import from source.

So this closes the loop: whatever `src/gui.py` defers, the spec must list.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "src" / "gui.py"
SPEC = ROOT / "IATACodeValidator.spec"


def _deferred_imports() -> set:
    """`from . import X` written inside a function, not at module level."""
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    top = {id(n) for n in tree.body}
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 1 or node.module is not None:
            continue                      # `from .x import y` is not this
        if id(node) in top:
            continue                      # module-level: analysed normally
        out.update(alias.name for alias in node.names)
    return out


def _spec_hidden() -> set:
    text = SPEC.read_text(encoding="utf-8")
    return set(re.findall(r'"src\.([a-z_]+)"', text))


def test_the_gui_defers_some_imports_so_this_test_has_something_to_check():
    assert len(_deferred_imports()) >= 10


def test_every_deferred_gui_import_is_named_in_the_spec():
    missing = sorted(_deferred_imports() - _spec_hidden())
    assert not missing, (
        "src/gui.py reaches these with a deferred `from . import`, but "
        f"IATACodeValidator.spec does not list them: {missing}. "
        "Add them to hiddenimports -- a missed one loses the tab silently in "
        "the packaged .exe while every test still passes.")


def test_the_spec_only_names_modules_that_exist():
    """A renamed or deleted module left in the spec fails the build late."""
    gone = sorted(name for name in _spec_hidden()
                  if not (ROOT / "src" / f"{name}.py").is_file())
    assert not gone, f"spec names modules that no longer exist: {gone}"

"""Regression test: every local Python module imported by the engine must be
listed in app/build.gradle's syncPythonEngine task, otherwise the APK ships
without it and crashes with ModuleNotFoundError at runtime.
"""

import ast
import os
import re
import sys

REPO = os.path.join(os.path.dirname(__file__), "..")


def _engine_files():
    gradle = open(os.path.join(REPO, "app", "build.gradle")).read()
    match = re.search(r"def engineFiles = \[(.*?)\]", gradle, re.S)
    assert match, "engineFiles list not found in app/build.gradle"
    return re.findall(r'"([^"]+)"', match.group(1))


def _local_imports(py_path):
    tree = ast.parse(open(py_path).read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
    # Only modules that exist as sibling .py files (not stdlib/third-party).
    local = set()
    root = os.path.dirname(py_path)
    for mod in mods:
        if os.path.isfile(os.path.join(root, mod + ".py")):
            local.add(mod)
    return local


def test_all_local_imports_are_synced():
    engine_files = _engine_files()
    synced_mods = {f[:-3] for f in engine_files if f.endswith(".py")}
    missing = {}
    for fname in engine_files:
        path = os.path.join(REPO, fname)
        if not os.path.isfile(path):
            continue
        for mod in _local_imports(path):
            if mod not in synced_mods:
                missing.setdefault(fname, []).append(mod)
    assert not missing, (
        "Local modules imported but NOT synced into the APK "
        f"(would crash with ModuleNotFoundError): {missing}"
    )


def test_stringpacks_is_synced():
    assert "stringpacks.py" in _engine_files()

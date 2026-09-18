"""Mechanical enforcement of the file and function size limits.

Ruff's PLR0915 counts statements rather than lines, so the line-based limits live here,
where they run with the suite everyone already runs and name the offending files.
"""

from __future__ import annotations

import ast
from pathlib import Path

FILE_HARD_LIMIT = 1000
FILE_SOFT_LIMIT = 600
FUNCTION_HARD_LIMIT = 100

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "dev_cloud"


def _source_files() -> list[Path]:
    return sorted(p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_source_files_exist() -> None:
    """Guard the guard: a glob that matches nothing would make the limits vacuous."""
    assert len(_source_files()) > 10


def test_no_source_file_exceeds_the_hard_limit() -> None:
    over = []
    for path in _source_files():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > FILE_HARD_LIMIT:
            over.append(f"{path.relative_to(SOURCE_ROOT)} - {lines} lines")
        elif lines > FILE_SOFT_LIMIT:
            # Not a failure: surfaces the seam while splitting is still cheap.
            print(f"note: {path.relative_to(SOURCE_ROOT)} is {lines} lines")

    assert not over, "files over the size limit:\n" + "\n".join(over)


def test_no_function_exceeds_the_hard_limit() -> None:
    over = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno
            if length > FUNCTION_HARD_LIMIT:
                over.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno} {node.name} "
                            f"- {length} lines")

    assert not over, "functions over the size limit:\n" + "\n".join(over)

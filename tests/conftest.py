"""pytest glue for a suite that was written before pytest.

Most files here are plain scripts: they run their assertions at import time
and end with ``sys.exit(0 if ok else 1)``.  Under pytest that top-level exit
raises ``SystemExit`` during collection, which pytest reports as a collection
ERROR and aborts the whole run -- so ``pytest tests`` used to be unusable no
matter how ``python_files`` was configured.

This hook skips those script-style modules *only* for pytest; ``tests/run_all.py``
still runs them exactly as before, and each one still works when executed
directly with ``python tests/_test_x.py``.
"""

import ast
import pathlib


def _exits_at_import(body) -> bool:
    """True when a statement list ends the process while the module is imported.

    Nested ``if``/``try``/``with`` blocks count (a conditional final
    ``sys.exit(1)`` is still a module-scope exit); function and class bodies
    do not, since those only run when called.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        # a bare call sits inside an Expr statement; tolerate both shapes
        target = node.value if isinstance(node, ast.Expr) else node
        if isinstance(target, ast.Call):
            func = target.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "sys"
                and func.attr == "exit"
            ):
                return True
        for field in ("body", "orelse", "finalbody", "handlers"):
            inner = getattr(node, field, None)
            if isinstance(inner, list) and inner and _exits_at_import(inner):
                return True
    return False


def _is_script_style(path: pathlib.Path) -> bool:
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return False
    return _exits_at_import(tree.body)


def pytest_ignore_collect(collection_path, config):
    path = pathlib.Path(str(collection_path))
    if path.suffix != ".py":
        return None
    return True if _is_script_style(path) else None

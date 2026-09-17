"""Architectural rules, enforced.

These two constraints are what keep the state machine testable without a GUI or
an optical drive. They are easy to violate accidentally with a convenience
import, and the cost only shows up much later as untestable code - so they are
checked here rather than trusted.
"""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "backupov2"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def python_files(*, exclude_dir: str | None = None) -> list[Path]:
    files = []
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if exclude_dir and exclude_dir in path.relative_to(PACKAGE).parts:
            continue
        files.append(path)
    return files


class LayeringTests(unittest.TestCase):
    def test_only_the_ui_package_imports_tkinter(self) -> None:
        for path in python_files(exclude_dir="ui"):
            with self.subTest(module=path.name):
                self.assertNotIn(
                    "tkinter",
                    imported_modules(path),
                    f"{path.name} imports tkinter; the runner must stay GUI-free "
                    "so whole disc sessions can be replayed in tests.",
                )

    def test_only_winapi_imports_ctypes(self) -> None:
        for path in python_files():
            if path.name == "winapi.py":
                continue
            with self.subTest(module=path.name):
                self.assertNotIn(
                    "ctypes",
                    imported_modules(path),
                    f"{path.name} imports ctypes; all Win32 access belongs in "
                    "winapi.py so it can be faked.",
                )

    def test_runner_does_not_import_the_ui(self) -> None:
        tree = ast.parse((PACKAGE / "runner.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("ui", node.module.split("."))

    def test_thread_targets_never_touch_tk(self) -> None:
        """A worker thread must hand results back through a queue.

        Calling ``self.after(...)`` - or any widget method - from a non-main
        thread raises "main thread is not in main loop" and, worse, can corrupt
        the interpreter when a mainloop *is* running. Every background function
        in the UI must therefore only put things on a queue that the main thread
        pumps, which is how the runner reports back to the app.
        """
        for path in PACKAGE.glob("ui/*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))

            # Functions handed to threading.Thread(target=...)
            targets: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "attr", getattr(func, "id", ""))
                if name != "Thread":
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "target" and isinstance(keyword.value, ast.Name):
                        targets.add(keyword.value.id)

            if not targets:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name not in targets:
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                        with self.subTest(module=path.name, func=node.name):
                            self.assertNotEqual(
                                inner.func.attr,
                                "after",
                                f"{path.name}:{node.name} calls .after() off the main "
                                "thread; put the result on a queue instead.",
                            )

    def test_every_module_parses(self) -> None:
        for path in python_files():
            with self.subTest(module=path.name):
                ast.parse(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

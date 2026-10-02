"""Import direction between app/ packages, checked statically.

    server.py -> handlers -> web -> services / providers / prompts / utils

Model-calling code (services, providers, prompts) never reaches up into the
web layer, so it stays usable with no socket or Session — the property that
keeps a separately hosted model gateway cheap if one is ever wanted. And
web/ never imports handlers/, which is what lets handler modules import the
shared senders in web/ without a cycle.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path) -> set[str]:
    """Absolute module names imported by `path`, with relative imports resolved."""
    package = _module_name(path) if path.name == "__init__.py" else _module_name(path).rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            found.add(prefix)
            # `from . import runtime` imports a module, not a name in a package.
            found.update(f"{prefix}.{alias.name}" for alias in node.names)
    return found


def _files(*packages: str) -> list[Path]:
    return [p for pkg in packages for p in sorted((APP / pkg).rglob("*.py"))]


def _violations(files: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{f.relative_to(APP.parent)} imports {name}"
        for f in files
        for name in sorted(_imports(f))
        if any(name == bad or name.startswith(bad + ".") for bad in forbidden)
    ]


@pytest.mark.parametrize("package", ["services", "providers", "prompts", "utils"])
def test_lower_layers_never_import_the_web_layer(package):
    assert _violations(_files(package), ("app.server", "app.web", "app.handlers")) == []


def test_web_never_imports_handlers():
    assert _violations(_files("web"), ("app.server", "app.handlers")) == []


@pytest.mark.parametrize("package", ["providers", "prompts"])
def test_model_code_only_touches_services_through_errors(package):
    """Providers and prompts carry no repo, session or settings knowledge —
    the shared error type is the only service they may import."""
    allowed = ("app.services", "app.services.errors")
    offending = [
        f"{f.relative_to(APP.parent)} imports {name}"
        for f in _files(package)
        for name in sorted(_imports(f))
        if name.startswith("app.services.") and name not in allowed and not name.startswith("app.services.errors.")
    ]
    assert offending == []

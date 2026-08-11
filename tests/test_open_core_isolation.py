"""Enforce the open-core import boundary between community and commercial code.

WHY THIS EXISTS
---------------
The community repo must stay runnable on its own — a self-hosted user who has
*not* purchased Pro (and certainly has no access to the proprietary Cloud
package) installs only ``validibot`` and must be able to import every module
and boot the app. The commercial packages are activated by *adding them to
``INSTALLED_APPS``* (see ``config/settings/local_pro.py``), not by being
imported from community code.

Because our day-to-day test venv has ``validibot_pro`` / ``validibot_cloud``
installed (so ``just test pro`` / ``just test cloud`` can run), a stray import
of a commercial package from community source would NOT fail locally — it would
only blow up in a real community install. This test is the safety net that
makes that regression fail here instead.

THE THREE RULES IT ENFORCES
---------------------------
1. **The existing Pro-import baseline cannot grow.** Older credential and URL
   integration paths use deferred imports of ``validibot_pro``. Every current
   occurrence is recorded below so new imports fail CI and removed imports
   require the baseline to ratchet downward. New commercial integrations use
   community-owned registries, protocols, or hooks instead.
2. **Allowlisted Pro imports must remain conditional.** A bare module-level
   ``from validibot_pro import …`` would raise ``ModuleNotFoundError`` on a
   community-only installation, so even a known integration path must remain
   inside a function, ``try`` block, or explicit conditional.
3. **Cloud must never be referenced at all.** The one-way rule (see
   ``validibot-cloud/CLAUDE.md``): cloud may import community, never the
   reverse. So *any* ``validibot_cloud`` import in community source — lazy or
   not — is a violation.

If this test fails, move the commercial behavior behind a community-owned
extension interface or remove the community→cloud dependency entirely.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

# Repo root is the parent of this ``tests/`` directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Community source trees to scan. These are the parts a community-only install
# ships and imports; tests and migrations are excluded below.
_SCAN_ROOTS = (
    _REPO_ROOT / "validibot",
    _REPO_ROOT / "config",
    _REPO_ROOT / "mcp" / "src",
)

_PRO_PREFIX = "validibot_pro"
_CLOUD_PREFIX = "validibot_cloud"

# Temporary migration baseline. Keys are stable ``(path, imported module)``
# pairs rather than line numbers so harmless formatting does not churn the
# contract. Counts preserve repeated imports from the same module in one file.
# Removing an import must lower this baseline in the same change.
_ALLOWED_PRO_IMPORTS = Counter(
    {
        ("config/urls_web.py", "validibot_pro.urls"): 1,
        (
            "validibot/validations/api_views.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/validations/credential_utils.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/validations/management/commands/delete_validation_runs.py",
            "validibot_pro.credentials.models",
        ): 4,
        (
            "validibot/validations/serializers.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/validations/services/evidence_bundle.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/validations/views/runs.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/workflows/views/management.py",
            "validibot_pro.credentials.models",
        ): 1,
        (
            "validibot/workflows/views/management.py",
            "validibot_pro.credentials.workflow_digest",
        ): 1,
    },
)


def _is_excluded(path: Path) -> bool:
    """Skip tests, migrations, caches — they may legitimately import commercial code."""
    parts = set(path.parts)
    if parts & {"tests", "migrations", "__pycache__"}:
        return True
    return path.name.startswith("test_") or path.name == "conftest.py"


def _iter_community_py_files():
    """Yield every community ``.py`` source file in scope."""
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not _is_excluded(path):
                yield path


def _references(module: str | None, prefix: str) -> bool:
    """Return True when an imported module name is (or is under) ``prefix``."""
    if not module:
        return False
    return module == prefix or module.startswith(f"{prefix}.")


def _is_deferred(node: ast.AST) -> bool:
    """Return True when *node*'s import does not run *unconditionally* at import.

    An import is safe for a community-only install when it sits inside any of:

    * a **function** body — only runs when that code path executes;
    * a ``try`` block — guarded against the package being absent;
    * an ``if`` block — conditional. Existing allowlisted module-level examples
      include ``if "validibot_pro" in settings.INSTALLED_APPS:`` (see
      ``config/urls_web.py``) and ``if TYPE_CHECKING:`` for type-only imports.

    Only a *bare, unconditional* module-level import would always execute (and
    therefore crash) when Pro is absent, so that is the only shape we forbid.
    """
    cur = getattr(node, "parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Try, ast.If)):
            return True
        cur = getattr(cur, "parent", None)
    return False


def _imported_modules(node: ast.Import | ast.ImportFrom):
    """Yield the dotted module name(s) an import node brings in."""
    if isinstance(node, ast.ImportFrom):
        # Ignore relative imports (level > 0) — they can't reach a top-level
        # commercial package.
        if node.level == 0:
            yield node.module
    else:
        for alias in node.names:
            yield alias.name


def _commercial_import_violations() -> list[str]:
    """Collect every community import that breaks the open-core boundary."""
    violations: list[str] = []
    observed_pro_imports: Counter[tuple[str, str]] = Counter()
    for path in _iter_community_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        # Attach parent pointers so ``_is_deferred`` can climb the tree.
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]

        rel = path.relative_to(_REPO_ROOT)
        rel_text = rel.as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(node):
                if _references(module, _CLOUD_PREFIX):
                    violations.append(
                        f"{rel}:{node.lineno}: community must never import "
                        f"{module!r} (one-way rule: cloud→community only)",
                    )
                elif _references(module, _PRO_PREFIX) and not _is_deferred(node):
                    violations.append(
                        f"{rel}:{node.lineno}: top-level import of {module!r}; "
                        "commercial providers must register through a "
                        "community-owned extension interface",
                    )
                elif _references(module, _PRO_PREFIX):
                    observed_pro_imports[(rel_text, module)] += 1

    for (path, module), count in (observed_pro_imports - _ALLOWED_PRO_IMPORTS).items():
        allowed = _ALLOWED_PRO_IMPORTS[(path, module)]
        violations.append(
            f"{path}: found {count + allowed} deferred imports of {module!r}, "
            f"but the migration baseline allows {allowed}; use a "
            "community-owned extension interface",
        )
    for (path, module), count in (_ALLOWED_PRO_IMPORTS - observed_pro_imports).items():
        observed = observed_pro_imports[(path, module)]
        violations.append(
            f"{path}: Pro-import debt decreased for {module!r}: baseline allows "
            f"{count + observed}, observed {observed}; ratchet "
            "_ALLOWED_PRO_IMPORTS downward",
        )
    return violations


def test_community_commercial_imports_do_not_exceed_migration_baseline():
    """Community source must not add knowledge of downstream packages.

    Existing deferred Pro imports remain community-install safe but are tracked
    debt, not precedent. Cloud imports and unconditional Pro imports remain
    forbidden. A failure identifies either new debt or a stale baseline that
    should be ratcheted after an import was removed.
    """
    violations = _commercial_import_violations()
    detail = "\n  ".join(violations)
    assert not violations, f"Open-core boundary violations:\n  {detail}"

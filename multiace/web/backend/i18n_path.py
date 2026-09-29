"""Locate multiACE translation catalogs across supported install layouts."""
from __future__ import annotations

import os
from pathlib import Path


def resolve_i18n_dir(module_file: str | os.PathLike[str]) -> Path:
    """Return the catalog directory for a backend module.

    Managed packages keep shared provider data at the application root while
    the standalone installer historically copied catalogs into ``web/i18n``.
    Prefer the application-root layout and retain the standalone layout as a
    fallback so both installations use the same backend code.
    """
    configured = os.environ.get("MULTIACE_I18N_DIR", "").strip()
    if configured:
        return Path(configured)

    backend_dir = Path(module_file).resolve().parent
    web_dir = backend_dir.parent
    package_dir = web_dir.parent
    candidates = (package_dir / "i18n", web_dir / "i18n")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]

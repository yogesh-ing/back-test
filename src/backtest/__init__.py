from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

__all__ = ["__version__"]
__version__ = "0.1.0"


def _load_project_env() -> None:
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_dotenv(dotenv_path=resolved, override=False)


_load_project_env()

# Keep the project environment consistent even if the caller imported a module directly
if not os.getenv("MSTOCK_BASE_URL"):
    os.environ["MSTOCK_BASE_URL"] = "https://api.mstock.trade"

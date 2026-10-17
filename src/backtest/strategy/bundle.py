"""Transitive strategy-bundle fingerprint (Refactor Plan v2.1, §8, Phase 0).

A spawn snapshot freezes *configuration*; it cannot freeze the Python
code a runner will load after a restart. The bundle hash therefore covers
the minimum transitive surface that determines strategy-side behaviour —
the strategy's own file, every module in its template/base class MRO, and
``strategy/execution.py`` — so that editing a template visibly changes the
fingerprint of *every* strategy inheriting from it.

Phase 0 provides computation + comparison. Phase 1 wires the comparison
into runner rehydration: a mismatch starts the runner PAUSED with the
``STRATEGY_CODE_CHANGED`` state (``strategy.execution``) until an operator
consciously migrates it. Engine-side code (bridge, gate, selectors) is
covered once per process by the engine git SHA, not per strategy.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "BundleFile",
    "BundleFingerprint",
    "strategy_bundle",
    "bundle_matches",
    "engine_git_sha",
]


@dataclass(frozen=True)
class BundleFile:
    path: str  # repo-relative where determinable, else module file path
    sha256: str


@dataclass(frozen=True)
class BundleFingerprint:
    files: tuple[BundleFile, ...]
    sha256: str  # aggregate over the canonical (path, sha256) list

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_sha256": self.sha256,
            "bundle_files": [
                {"path": f.path, "sha256": f.sha256} for f in self.files
            ],
        }


def _repo_root_for(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    # Canonical '/' separators on every platform: bundle_sha256 aggregates
    # these strings, so a snapshot stamped on Linux must recompute to the
    # same hash on Windows (Path.as_posix, never str(Path), here).
    root = _repo_root_for(path)
    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _module_files(cls: type) -> list[Path]:
    """Files determining ``cls``'s strategy-side behaviour (see module docstring).

    Only files inside the repository checkout participate: the MRO of any
    strategy reaches into the stdlib (``abc``, ``typing``...), and hashing
    the interpreter's library would make the fingerprint depend on the
    Python installation rather than on our code. If no repo root can be
    determined (installed package), all local files participate.
    """
    modules: list[Any] = []
    for klass in cls.__mro__:
        module = inspect.getmodule(klass)
        if module is not None and module not in modules:
            modules.append(module)
    # The execution contract module always participates: every spec is
    # parsed/validated by it, so its semantics are part of the bundle.
    from backtest.strategy import execution as execution_module

    if execution_module not in modules:
        modules.append(execution_module)

    candidates: list[Path] = []
    for module in modules:
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = Path(file)
        if path.suffix == ".pyc":
            path = path.with_suffix(".py")
        if path.is_file():
            candidates.append(path)

    root: Path | None = None
    try:
        own = inspect.getfile(cls)
    except TypeError:
        # A plugin class whose module was evicted from sys.modules by a
        # re-import-on-change cycle: the class is orphaned, but its MRO
        # modules (templates, execution) still fingerprint — an orphan
        # must not make the strategy unspawnable (Phase 4 fail-closed
        # stamping turned this latent gap into a spawn refusal).
        own = None
    if own:
        root = _repo_root_for(Path(own))
    if root is not None:
        resolved_root = root.resolve()
        candidates = [
            path
            for path in candidates
            if resolved_root in path.resolve().parents
            or path.resolve().parent == resolved_root
        ]
    # Canonical order: by display path, so the aggregate is reproducible
    # regardless of MRO/discovery order.
    unique = {str(path): path for path in candidates}
    return sorted(unique.values(), key=_display_path)


def strategy_bundle(cls: type) -> BundleFingerprint:
    """Compute the transitive bundle fingerprint for a strategy class."""
    files = tuple(
        BundleFile(path=_display_path(path), sha256=_hash_file(path))
        for path in _module_files(cls)
    )
    aggregate = hashlib.sha256()
    for entry in files:
        aggregate.update(f"{entry.path}\0{entry.sha256}\n".encode("utf-8"))
    return BundleFingerprint(files=files, sha256=aggregate.hexdigest())


def bundle_matches(cls: type, snapshot_files: Iterable[MappingLike]) -> bool:
    """True iff the code ``cls`` would load now matches a spawn snapshot.

    ``snapshot_files`` is the ``bundle_files`` list stored at spawn (§8).
    Any difference — edited template, added/removed module — is a mismatch.
    """
    current = strategy_bundle(cls)
    stored = sorted(
        (str(entry["path"]), str(entry["sha256"])) for entry in snapshot_files
    )
    return [(f.path, f.sha256) for f in current.files] == stored


# A Mapping-shaped dict; kept unparameterised to avoid a typing import cycle.
MappingLike = Any


def engine_git_sha(cwd: str | Path | None = None) -> str | None:
    """Best-effort git SHA of the running engine code (§8: ``engine_git_sha``).

    Returns ``None`` outside a git checkout — honesty over invention.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 — fingerprinting must never break startup
        return None
    sha = (out.stdout or "").strip()
    return sha or None

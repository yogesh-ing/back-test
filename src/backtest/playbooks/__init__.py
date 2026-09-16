"""Playbooks package — canonical location per unified architecture.

Re-exports models and registry for convenience.
"""

from backtest.playbooks.models import Playbook
from backtest.playbooks.registry import (
    PlaybookRegistry,
    get_playbook_registry,
    reset_playbook_registry,
)

__all__ = ["Playbook", "PlaybookRegistry", "get_playbook_registry", "reset_playbook_registry"]

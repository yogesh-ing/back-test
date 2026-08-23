"""Real-time Dashboard for forward testing (Step 19)."""

from .app import create_dashboard_app, run_dashboard
from .data_provider import DashboardDataProvider

__all__ = ["create_dashboard_app", "run_dashboard", "DashboardDataProvider"]

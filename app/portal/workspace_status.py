"""Workspace application availability displayed on the portal dashboard."""

from app.models import GoogleConnection


_SCOPE_MARKERS = {
    "gmail": ("gmail", "mail.google.com"),
    "calendar": ("calendar",),
    "drive": ("drive",),
    "docs": ("documents", "docs", "drive"),
    "sheets": ("spreadsheets", "sheets", "drive"),
    "slides": ("presentations", "drive"),
}


def workspace_app_statuses(connection: GoogleConnection | None) -> dict[str, bool]:
    scopes = (connection.scopes or "") if connection and connection.status == "connected" else ""
    return {
        app_id: any(marker in scopes for marker in markers)
        for app_id, markers in _SCOPE_MARKERS.items()
    }

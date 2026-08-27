"""
Shared helper for recording activity_log entries.

Logging is a side-effect, not the main action — the same reasoning as
notifications in routers/budget.py applies here (see utils/db_safety.py's
docstring). A failure writing the log entry (e.g. before the migration has
been run) must never take down the approve/reject/import/etc. it's attached
to. So this always goes through run_safely internally; callers just call
log_activity(...) directly with no extra wrapping needed.
"""
from core.database import execute
from utils.db_safety import run_safely

# Short machine-readable action codes, for future filtering by type.
ACTION_BUDGET_SUBMITTED = "budget_submitted"
ACTION_BUDGET_APPROVED = "budget_approved"
ACTION_BUDGET_REJECTED = "budget_rejected"
ACTION_EVENT_IMPORTED = "event_imported"
ACTION_BUDGET_IMPORTED = "budget_imported"
ACTION_EVENT_STATUS_CHANGED = "event_status_changed"
ACTION_TASK_ASSIGNED = "task_assigned"
ACTION_TASK_UPDATED = "task_updated"


def log_activity(conn, event_id: int, user_id, action: str, description: str):
    """Records one activity_log row. Safe to call from inside a larger
    request handler — wrapped in its own SAVEPOINT so it can never roll
    back the caller's real work."""
    def _write():
        execute(
            conn,
            "INSERT INTO activity_log (event_id, user_id, action, description) VALUES (%s,%s,%s,%s)",
            (event_id, user_id, action, description),
        )
    run_safely(conn, _write)

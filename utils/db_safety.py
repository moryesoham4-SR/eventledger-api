"""
Shared helper for side-effects that must never take down the real action
they're attached to.

PostgreSQL aborts the WHOLE transaction the instant any single statement
fails, and refuses every further command — including the connection's final
commit in get_db() — until a rollback happens. So a secondary effect (a
notification, a synced expense/income row) failing because of an unexpected
schema detail must not be allowed to undo the primary action that already
succeeded earlier in the same request.
"""

def run_safely(conn, fn):
    """Runs `fn()` inside its own SAVEPOINT. On failure, rolls back only to
    that savepoint (undoing just `fn()`'s work) and logs it, leaving the
    rest of the transaction intact for the final commit."""
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT action_sp")
        fn()
        cur.execute("RELEASE SAVEPOINT action_sp")
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT action_sp")
        print(f"[run_safely] non-critical step failed, rolled back to savepoint: {exc}")

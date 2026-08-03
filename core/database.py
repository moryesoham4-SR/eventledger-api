"""
Database connection pool — reused across all requests.
"""
import psycopg2
import psycopg2.pool
import psycopg2.extras
from core.config import settings

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=settings.DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool

def get_db():
    """FastAPI dependency — yields a DB connection, returns it to pool after."""
    pool = get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

def execute(conn, sql: str, params=None):
    """Helper to run a query and return results."""
    # Convert ? to %s for PostgreSQL
    import re
    sql = re.sub(r'\?', '%s', sql)
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    return cur

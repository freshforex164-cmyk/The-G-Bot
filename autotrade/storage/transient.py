"""Connectivity failures only: programming and integrity errors must stay visible."""
import psycopg
from psycopg_pool import PoolTimeout


def is_transient(error: Exception) -> bool:
    if isinstance(error, (PoolTimeout, ConnectionError, TimeoutError)):
        return True
    if isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError)):
        state = error.sqlstate
        return state is None or state.startswith("08") or state in ("57P01", "57P02", "57P03")
    return False
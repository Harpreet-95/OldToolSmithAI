"""
Independent read-only SQL verification helper — REAL CCPP Demo Validation
milestone, Part 5/13. TEST CODE ONLY: never imported by production planning.

Opens a direct connection to the live CCPP source using the exact same
connector/config-decryption mechanism data.query_execution_service uses
internally (data.datasource_service.get_connection_config +
core.connectors.registry), then runs plain read-only SELECT statements this
script authors by hand — independent of anything the AI planner produced —
so a match between ToolSmithAI's answer and this script's own query is real
corroboration, not the same code checking itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.connectors.relational.mssql       # noqa: E402,F401
import core.connectors.relational.mysql       # noqa: E402,F401
import core.connectors.relational.postgresql  # noqa: E402,F401


def open_live_connection(source_id: int, user_id: str):
    import json
    import core.connectors.registry as _registry
    from core.connectors.base import DataSourceConfig
    from core.secrets.manager import get_secret_manager
    from data.db import get_connection

    meta_conn = get_connection()
    try:
        row = meta_conn.execute(
            "SELECT source_type, encrypted_config_json "
            "FROM data_source_connections WHERE id = ? AND user_id = ?",
            (source_id, str(user_id)),
        ).fetchone()
    finally:
        meta_conn.close()
    if row is None:
        raise PermissionError(f"source_id={source_id} not found/owned by user_id={user_id}")

    source_type = row["source_type"]
    params = json.loads(get_secret_manager().decrypt_secret(row["encrypted_config_json"]))
    connector_cls = _registry.get(source_type)
    config = DataSourceConfig(source_type=source_type, params=params)
    return connector_cls().open_connection(config)


def run_sql(conn, sql: str) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
    return rows


if __name__ == "__main__":
    conn = open_live_connection(1, "28")
    print(run_sql(conn, "SELECT COUNT(*) AS n FROM dbo.ADF_Student"))

"""MySQL READ ONLY snapshot。書込みSQLは拒否する。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import mysql.connector
from mysql.connector.abstracts import MySQLConnectionAbstract

from db2rag.textnorm import normalize_text, sha256_text

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"INTO\s+OUTFILE|INTO\s+DUMPFILE|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|"
    r"LOAD_FILE|SLEEP\s*\()\b",
    re.IGNORECASE,
)


class MysqlError(RuntimeError):
    pass


def _ident(value: str) -> str:
    if not _IDENT.match(value):
        raise MysqlError("ERR_MYSQL_BAD_IDENTIFIER")
    return value


def assert_readonly_sql(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise MysqlError("ERR_MYSQL_MULTI_STATEMENT")
    if FORBIDDEN.search(stripped):
        raise MysqlError("ERR_MYSQL_WRITE_SQL")
    if not re.match(r"^(WITH|SELECT)\b", stripped, re.IGNORECASE):
        raise MysqlError("ERR_MYSQL_NOT_SELECT")
    return stripped


def connect(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    ssl_mode: str = "DISABLED",
    timeout: int = 30,
) -> MySQLConnectionAbstract:
    ssl_disabled = ssl_mode.upper() in {"DISABLED", "OFF", "NONE"}
    kwargs: dict[str, Any] = dict(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        use_unicode=True,
        autocommit=False,
        connection_timeout=timeout,
        ssl_disabled=ssl_disabled,
    )
    try:
        return mysql.connector.connect(**kwargs)
    except mysql.connector.Error as exc:
        # caching_sha2_password は認証キャッシュが冷えている（MySQL 再起動直後）と
        # 平文チャネルの full auth で 1045 を返す。TLS で再試行して full auth を通す。
        # パスワード誤りなら TLS でも 1045 になるので、元のエラーをそのまま返す。
        if not ssl_disabled or getattr(exc, "errno", None) != 1045:
            raise
        try:
            conn = mysql.connector.connect(**{**kwargs, "ssl_disabled": False})
        except mysql.connector.Error:
            raise exc from None
        return conn




def test_connection(conn: MySQLConnectionAbstract) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute("SELECT 1")
    cursor.fetchone()
    cursor.execute("SHOW VARIABLES LIKE 'version'")
    version_row = cursor.fetchone()
    version = version_row[1] if version_row else "unknown"
    write_blocked = True
    try:
        cursor.execute("INSERT INTO regulations (id, code, title, body, acl_tag) VALUES (-1, 'probe', 'probe', 'probe', 'x')")
        conn.rollback()
        write_blocked = False
    except Exception:
        write_blocked = True
        try:
            conn.rollback()
        except Exception:
            pass
    cursor.close()
    warning = None
    if str(version).startswith("8.0"):
        warning = "MySQL 8.0.x is supported with warning"
    if str(version).startswith("5.7"):
        raise MysqlError("ERR_MYSQL_UNSUPPORTED_VERSION")
    conn.commit()
    return {
        "ok": True,
        "version": version,
        "write_blocked": write_blocked,
        "warning": warning,
    }


def list_objects(conn: MySQLConnectionAbstract, database: str) -> list[dict[str, Any]]:
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name, TABLE_TYPE AS table_type,
               TABLE_ROWS AS table_rows
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
        """,
        (database,),
    )
    tables = list(cursor.fetchall())
    objects: list[dict[str, Any]] = []
    for table in tables:
        pk = _primary_key(conn, table["table_schema"], table["table_name"])
        objects.append(
            {
                "object_key": f"{table['table_schema']}.{table['table_name']}",
                "object_type": "view" if table["table_type"] == "VIEW" else "table",
                "pk_columns": pk,
                "estimated_rows": int(table["table_rows"] or 0),
                "selectable": bool(pk),
            }
        )
    cursor.close()
    conn.commit()
    return objects


def _primary_key(conn: MySQLConnectionAbstract, schema: str, table: str) -> list[str]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY'
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    cols = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return cols


def iter_table_rows(
    conn: MySQLConnectionAbstract,
    schema: str,
    table: str,
) -> Iterator[dict[str, Any]]:
    schema = _ident(schema)
    table = _ident(table)
    if getattr(conn, "in_transaction", False):
        conn.commit()
    conn.start_transaction(
        consistent_snapshot=True,
        isolation_level="REPEATABLE READ",
        readonly=True,
    )
    cursor = conn.cursor(dictionary=True, buffered=False)
    cursor.execute(f"SELECT * FROM `{schema}`.`{table}`")
    for row in cursor:
        yield {str(key): _coerce(value) for key, value in row.items()}
    cursor.close()
    conn.commit()


def render_record(title: str, record_id: str, row: dict[str, Any]) -> str:
    bullets: list[str] = []
    long_parts: list[str] = []
    for key, value in row.items():
        text = normalize_text("" if value is None else str(value))
        if not text:
            continue
        if len(text) >= 80:
            long_parts.append(f"## {key}\n{text}")
        else:
            bullets.append(f"- {key}: {text}")
    lines = [f"# {title}（{record_id}）", "", "## 基本情報", *bullets]
    if long_parts:
        lines.extend(["", *long_parts])
    return "\n".join(lines)


def record_id_from(pk_columns: list[str], row: dict[str, Any]) -> str:
    if not pk_columns:
        raise MysqlError("ERR_MYSQL_NO_PK")
    parts = []
    for col in pk_columns:
        parts.append(f"{col}={row.get(col, '')}")
    return "|".join(parts)


def row_hash(row: dict[str, Any]) -> str:
    ordered = [normalize_text("" if row[k] is None else str(row[k])) for k in sorted(row)]
    return sha256_text("|".join(ordered))


def _coerce(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

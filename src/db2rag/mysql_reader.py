"""MySQL READ ONLY snapshot。書込みSQLは拒否する。"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any, cast

import mysql.connector
from mysql.connector.abstracts import MySQLConnectionAbstract

from db2rag.textnorm import normalize_text, sha256_text

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"INTO\s+OUTFILE|INTO\s+DUMPFILE|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE|"
    r"LOAD_FILE|SLEEP\s*\()\b",
    re.IGNORECASE,
)
MAX_MYSQL_COLUMNS = 100
DEFAULT_MAX_ROWS = 500_000
_DATE_JA = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


class MysqlError(RuntimeError):
    """MySQL コネクタが呼び出し側へ返すエラー。"""


def _ident(value: str) -> str:
    if not _IDENT.match(value):
        raise MysqlError("ERR_MYSQL_BAD_IDENTIFIER")
    return value


def assert_readonly_sql(sql: str) -> str:
    """SELECT / WITH だけを残し、書込みと複数文を拒否する。

    Args:
        sql: 利用者または保存済みの SQL。

    Returns:
        末尾のセミコロンを除いた SQL。

    Raises:
        MysqlError: 複数文、書込み、SELECT/WITH 以外。
    """
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
    """READ ONLY 想定の MySQL 接続を開く。

    Args:
        host: 接続先ホスト。
        port: ポート。
        user: ユーザー。
        password: パスワード。ログには出さない。
        database: データベース名。
        ssl_mode: ``DISABLED`` のとき平文。認証キャッシュが冷えていると TLS で再試行する。
        timeout: 接続タイムアウト秒。

    Returns:
        未コミットの接続。

    Raises:
        mysql.connector.Error: 接続できないとき。パスワード誤りは元の例外のまま。
    """
    ssl_disabled = ssl_mode.upper() in {"DISABLED", "OFF", "NONE"}
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "use_unicode": True,
        "autocommit": False,
        "connection_timeout": timeout,
        "ssl_disabled": ssl_disabled,
    }
    try:
        return cast(MySQLConnectionAbstract, mysql.connector.connect(**kwargs))
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
        return cast(MySQLConnectionAbstract, conn)



_WRITE_PRIV = re.compile(
    r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT OPTION|SUPER|FILE)\b",
    re.IGNORECASE,
)


def write_blocked_from_grants(grant_rows: list[str]) -> bool:
    """GRANT 文に書込み権限が無ければ True。

    Args:
        grant_rows: ``SHOW GRANTS`` の各行。

    Returns:
        書込み権限が無い、または空で判定できないとき True。
    """
    if not grant_rows:
        return True
    return all(not _WRITE_PRIV.search(row) for row in grant_rows)


def test_connection(conn: MySQLConnectionAbstract) -> dict[str, Any]:
    """疎通と版、書込み拒否を ``SHOW GRANTS`` で確認する。

    Args:
        conn: 開いている接続。

    Returns:
        ``ok`` / ``version`` / ``write_blocked`` / ``warning``。

    Raises:
        MysqlError: MySQL 5.7 のとき ``ERR_MYSQL_UNSUPPORTED_VERSION``。
    """
    cursor = conn.cursor()
    cursor.execute("SELECT 1")
    cursor.fetchone()
    cursor.execute("SHOW VARIABLES LIKE 'version'")
    version_row = cast(tuple[Any, ...] | None, cursor.fetchone())
    version = version_row[1] if version_row else "unknown"
    warning = None
    try:
        cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
        fetched = cast(list[tuple[Any, ...]], list(cursor.fetchall()))
        grant_rows = [str(row[0]) for row in fetched]
        write_blocked = write_blocked_from_grants(grant_rows)
    except Exception as exc:
        write_blocked = True
        warning = f"SHOW GRANTS failed: {exc}"
    cursor.close()
    if str(version).startswith("8.0"):
        version_warning = "MySQL 8.0.x is supported with warning"
        warning = f"{warning}; {version_warning}" if warning else version_warning
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
    """テーブルとビューを主キー付きで列挙する。

    Args:
        conn: 開いている接続。
        database: ``information_schema`` で絞るスキーマ名。

    Returns:
        ``object_key`` / ``pk_columns`` / ``selectable`` を持つ辞書のリスト。
    """
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
    tables = cast(list[dict[str, Any]], list(cursor.fetchall()))
    objects: list[dict[str, Any]] = []
    for table in tables:
        schema_name = str(table["table_schema"])
        table_name = str(table["table_name"])
        pk = _primary_key(conn, schema_name, table_name)
        objects.append(
            {
                "object_key": f"{schema_name}.{table_name}",
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
    fetched = cast(list[tuple[Any, ...]], list(cursor.fetchall()))
    cols = [str(row[0]) for row in fetched]
    cursor.close()
    return cols


def build_table_select(
    schema: str,
    table: str,
    *,
    where_col: str | None = None,
    since_value: str | None = None,
    lookups: list[dict[str, str]] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """テーブル全列と lookup の LEFT JOIN を、識別子検証済み SQL にする。

    Args:
        schema: ベース表のスキーマ。
        table: ベース表。
        where_col: 増分カーソル列。``since_value`` と組のときだけ WHERE を付ける。
        since_value: この値より大きい行だけ取る。プレースホルダにバインドする。
        lookups: ``column`` / ``lookup_table`` / ``lookup_key`` / ``lookup_label``。

    Returns:
        SQL とバインド値。WHERE が無いときは空タプル。

    Raises:
        MysqlError: 識別子が ``ERR_MYSQL_BAD_IDENTIFIER`` のとき。
    """
    schema_name = _ident(schema)
    table_name = _ident(table)
    join_sql, select_extra = _lookup_sql(schema_name, lookups or [])
    where_sql = ""
    params: tuple[Any, ...] = ()
    if where_col and since_value is not None:
        column = _ident(where_col)
        where_sql = f" WHERE `base`.`{column}` > %s ORDER BY `base`.`{column}` ASC"
        params = (since_value,)
    sql = (
        f"SELECT `base`.*{select_extra} FROM `{schema_name}`.`{table_name}` AS `base`"
        f"{join_sql}{where_sql}"
    )
    return sql, params


def _lookup_sql(schema: str, lookups: list[dict[str, str]]) -> tuple[str, str]:
    joins: list[str] = []
    labels: list[str] = []
    for index, spec in enumerate(lookups):
        column = _ident(str(spec.get("column") or ""))
        lookup_table = _ident(str(spec.get("lookup_table") or ""))
        lookup_key = _ident(str(spec.get("lookup_key") or ""))
        lookup_label = _ident(str(spec.get("lookup_label") or ""))
        alias = f"__lk_{index}"
        joins.append(
            f" LEFT JOIN `{schema}`.`{lookup_table}` AS `{alias}`"
            f" ON `base`.`{column}` = `{alias}`.`{lookup_key}`"
        )
        labels.append(f"`{alias}`.`{lookup_label}` AS `__label_{column}`")
    select_extra = (", " + ", ".join(labels)) if labels else ""
    return "".join(joins), select_extra


def iter_table_rows(
    conn: MySQLConnectionAbstract,
    schema: str,
    table: str,
    *,
    where_col: str | None = None,
    since_value: str | None = None,
    lookups: list[dict[str, str]] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> Iterator[dict[str, Any]]:
    """表を REPEATABLE READ で流す。全行リストは作らない。

    Args:
        conn: 開いている接続。
        schema: スキーマ名。
        table: 表名。
        where_col: 増分列。``since_value`` があるとき ``col > %s``。
        since_value: 増分カーソル。
        lookups: コード表の LEFT JOIN 定義。ラベルは ``__label_{column}``。
        max_rows: これを超えたら警告して打ち切る。

    Returns:
        行辞書のイテレータ。

    Raises:
        MysqlError: 識別子不正、または列数が 100 を超えるとき。
    """
    sql, params = build_table_select(
        schema,
        table,
        where_col=where_col,
        since_value=since_value,
        lookups=lookups,
    )
    yield from _stream_query(conn, sql, params, max_rows)


def iter_custom_sql_rows(
    conn: MySQLConnectionAbstract,
    sql: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> Iterator[dict[str, Any]]:
    """保存済み SQL を読取専用チェックのあとで流す。

    Args:
        conn: 開いている接続。
        sql: SELECT または WITH。
        max_rows: 超過分は警告して捨てる。

    Returns:
        行辞書のイテレータ。

    Raises:
        MysqlError: 読取専用でない、または列数が 100 を超えるとき。
    """
    safe_sql = assert_readonly_sql(sql)
    yield from _stream_query(conn, safe_sql, (), max_rows)


def _stream_query(
    conn: MySQLConnectionAbstract,
    sql: str,
    params: tuple[Any, ...],
    max_rows: int,
) -> Iterator[dict[str, Any]]:
    """unbuffered カーソルで行を yield する。終了時にカーソルを閉じる。"""
    if getattr(conn, "in_transaction", False):
        conn.commit()
    conn.start_transaction(
        consistent_snapshot=True,
        isolation_level="REPEATABLE READ",
        readonly=True,
    )
    cursor = conn.cursor(dictionary=True, buffered=False)
    try:
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        description = cursor.description or ()
        if len(description) > MAX_MYSQL_COLUMNS:
            raise MysqlError("ERR_MYSQL_TOO_MANY_COLUMNS")
        emitted = 0
        while True:
            row: Any = cursor.fetchone()
            if row is None:
                break
            if emitted >= max_rows:
                logger.warning("MySQL の取得行が max_rows=%s に達したため打ち切りました", max_rows)
                break
            emitted += 1
            yield {str(key): _coerce(value) for key, value in row.items()}
    finally:
        cursor.close()
        conn.commit()


def render_record(title: str, record_id: str, row: dict[str, Any]) -> str:
    """列を見出しと箇条書きにした既定の論理文書を返す。

    Args:
        title: 見出しに使うオブジェクトキー。
        record_id: 安定レコードキー。
        row: 列の値。空は落とす。80 文字以上は節本文にする。

    Returns:
        Markdown に近い本文。
    """
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


def render_record_with_template(
    object_key: str,
    record_id: str,
    row: dict[str, Any],
    template: dict[str, Any] | None,
) -> tuple[str, str, str | None, str | None]:
    """テンプレートで 1 行を論理文書にする。

    Args:
        object_key: ``schema.table``。
        record_id: 安定レコードキー。
        row: 列の値。lookup 結果は ``__label_{column}``。
        template: ``title_column`` / ``sections`` など。None なら既定描画。

    Returns:
        ``(本文, タイトル, updated_at の生値, 行 ACL)``。
        テンプレート無しのタイトルは ``"{object_key} {record_id}"``。
    """
    if template is None:
        text = render_record(object_key, record_id, row)
        return text, f"{object_key} {record_id}", None, None
    title = _cell_text(row, template.get("title_column")) or f"{object_key} {record_id}"
    updated_at = _cell_text(row, template.get("updated_at_column"))
    acl_tag = _cell_text(row, template.get("acl_column"))
    lines = [f"# {title}", ""]
    for section in template.get("sections") or []:
        if not isinstance(section, dict):
            continue
        body = _render_section(row, section)
        if not body:
            continue
        lines.extend(body)
        lines.append("")
    return "\n".join(lines).strip(), title, updated_at, acl_tag


def _render_section(row: dict[str, Any], section: dict[str, Any]) -> list[str]:
    body: list[str] = []
    for field in section.get("fields") or []:
        if not isinstance(field, dict):
            continue
        rendered = _render_field(row, field)
        if rendered:
            body.append(rendered)
    if not body:
        return []
    heading = normalize_text(str(section.get("heading") or ""))
    if heading:
        return [f"## {heading}", *body]
    return body


def _render_field(row: dict[str, Any], field: dict[str, Any]) -> str:
    column = field.get("column")
    if not column:
        return ""
    raw = row.get(str(column))
    if raw is None:
        return ""
    text = normalize_text(str(raw))
    if not text:
        return ""
    if field.get("format") == "date_ja":
        text = _format_date_ja(text)
    if field.get("lookup_table"):
        label = row.get(f"__label_{column}")
        label_text = normalize_text("" if label is None else str(label))
        if label_text:
            text = f"コード {text} → {label_text}"
    if field.get("type") == "long_text":
        # 箇条書きにせず、節の本文として置く。
        return text
    label = normalize_text(str(field.get("label") or column))
    return f"- {label}: {text}"


def _cell_text(row: dict[str, Any], column: Any) -> str | None:
    if not column:
        return None
    value = row.get(str(column))
    if value is None:
        return None
    text = normalize_text(str(value))
    return text or None


def _format_date_ja(value: str) -> str:
    """YYYY-MM-DD を年月日にする。解釈できなければ原文を返す。"""
    head = value.strip()
    matched = _DATE_JA.match(head)
    if not matched:
        return value
    year, month, day = int(matched.group(1)), int(matched.group(2)), int(matched.group(3))
    try:
        datetime(year, month, day)
    except ValueError:
        return value
    return f"{year}年{month}月{day}日"


def record_id_from(pk_columns: list[str], row: dict[str, Any]) -> str:
    """複合主キーを列名の昇順で連結する。

    Args:
        pk_columns: 主キー列。渡された順序は使わず ``sorted`` する。
        row: 行の値。欠損と NULL は空文字。

    Returns:
        ``a_id=1|b_id=2`` 形式の安定キー。

    Raises:
        MysqlError: 主キーが空のとき ``ERR_MYSQL_NO_PK``。
    """
    if not pk_columns:
        raise MysqlError("ERR_MYSQL_NO_PK")
    parts = [f"{col}={_record_token(row.get(col, ''))}" for col in sorted(pk_columns)]
    return "|".join(parts)


def _record_token(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def row_hash(row: dict[str, Any], column_order: list[str] | None = None) -> str:
    """行の安定ハッシュを返す。

    Args:
        row: 列の値。
        column_order: 明示順。省略時はキー昇順。

    Returns:
        ``col=value`` を ``|`` で繋いだ SHA-256 十六進。
    """
    columns = column_order if column_order is not None else sorted(row)
    parts = [f"{column}={_hash_token(row.get(column))}" for column in columns]
    return sha256_text("|".join(parts))


def _hash_token(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        stamped = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalize_text(stamped.astimezone(UTC).isoformat())
    if isinstance(value, date):
        return value.isoformat()
    return normalize_text(str(value))


def _coerce(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value

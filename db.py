"""SQL Server connection and schema migration for FSS Accounts."""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SQL_DIR = os.path.join(HERE, "database")


def _load_dotenv() -> None:
    path = os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()


def _pg_url_from_parts() -> str:
    from urllib.parse import quote_plus
    try:
        pg = load_config().get("postgres") or {}
    except Exception:
        pg = {}
    host = (os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST")
            or str(pg.get("host") or "")).strip()
    user = (os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER") or "").strip()
    password = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD") or ""
    port = (os.environ.get("PGPORT") or os.environ.get("POSTGRES_PORT")
            or str(pg.get("port") or "5432")).strip()
    database = (os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB")
                or str(pg.get("database") or "")).strip()
    if not (host and user and password and database):
        return ""
    ssl = (os.environ.get("PGSSLMODE") or "disable").strip()
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}?sslmode={ssl}"
    )


def supabase_url() -> str:
    return (
        os.environ.get("SUPABASE_DB_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or _pg_url_from_parts()
    ).strip()


def pg_connect_kwargs(url: str) -> dict:
    kw = {"connect_timeout": 20}
    if "sslmode=" in url.lower():
        return kw
    if "supabase.co" in url.lower():
        kw["sslmode"] = "require"
    else:
        kw["sslmode"] = "disable"
    return kw


def supabase_configured() -> bool:
    return bool(supabase_url())


def _is_vercel() -> bool:
    if os.name == "nt":
        return False
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def _local_servers() -> set[str]:
    return {"(local)", "localhost", ".", "(localdb)\\mssqllocaldb", "(localdb)\\mssqllocaldb"}


def _parse_odbc_connection_string(cs: str) -> dict[str, str]:
    """Parse Azure / ODBC connection strings into pymssql kwargs."""
    out: dict[str, str] = {}
    for part in cs.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = re.sub(r"\s+", "", key.lower())
        val = val.strip()
        if key in ("server", "datasource", "address", "addr", "networkaddress"):
            val = re.sub(r"^tcp:", "", val, flags=re.IGNORECASE)
            # pymssql accepts host or host:port
            out["host"] = val.split(",")[0] if "," in val else val
        elif key in ("initialcatalog", "database"):
            out["database"] = val
        elif key in ("userid", "uid", "user", "username"):
            out["user"] = val
        elif key in ("password", "pwd"):
            out["password"] = val
    return out


def _vercel_credentials(config: dict | None = None) -> tuple[str, str, str, str]:
    """Resolve SQL credentials for Vercel (env vars, connection string, or CONFIG_JSON)."""
    cfg = config or load_config()
    db_cfg = cfg.get("database", {})

    host = (os.environ.get("AZURE_SQL_HOST") or "").strip()
    user = (os.environ.get("AZURE_SQL_USER") or "").strip()
    password = os.environ.get("AZURE_SQL_PASSWORD") or ""
    database = (os.environ.get("AZURE_SQL_DATABASE") or db_cfg.get("database") or "FSSInvoice").strip()

    cs = (os.environ.get("AZURE_SQL_CONNECTION_STRING")
          or os.environ.get("SQL_CONNECTION_STRING") or "").strip()
    if cs:
        parsed = _parse_odbc_connection_string(cs)
        host = host or parsed.get("host", "")
        user = user or parsed.get("user", "")
        password = password or parsed.get("password", "")
        database = parsed.get("database") or database

    if not host:
        server = (db_cfg.get("server") or "").strip()
        if server and server.lower() not in {s.lower() for s in _local_servers()}:
            host = server
            user = user or (db_cfg.get("username") or "")
            password = password or (db_cfg.get("password") or "")

    if not host:
        raise RuntimeError(
            "Set AZURE_SQL_HOST + AZURE_SQL_USER + AZURE_SQL_PASSWORD on Vercel, "
            "or paste AZURE_SQL_CONNECTION_STRING from Azure Portal, "
            "or set CONFIG_JSON with a remote database section. See VERCEL_DEPLOY.md.")

    if not user or not password:
        raise RuntimeError(
            "Set AZURE_SQL_USER and AZURE_SQL_PASSWORD on Vercel (SQL login required).")

    return host, user, password, database


def vercel_db_configured() -> bool:
    try:
        _vercel_credentials()
        return True
    except RuntimeError:
        return False


def use_snapshot_fallback() -> bool:
    """Vercel without Azure SQL or Supabase — use bundled snapshot.json."""
    if not _is_vercel() or vercel_db_configured() or supabase_configured():
        return False
    try:
        import vercel_snapshot as vs
        return vs.snapshot_available()
    except ImportError:
        return False


def _import_pyodbc():
    import pyodbc
    return pyodbc


def load_config() -> dict:
    if os.environ.get("CONFIG_JSON"):
        try:
            return json.loads(os.environ["CONFIG_JSON"])
        except json.JSONDecodeError:
            pass
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def connection_string(config: dict | None = None) -> str:
    env_cs = os.environ.get("AZURE_SQL_CONNECTION_STRING") or os.environ.get("SQL_CONNECTION_STRING")
    if env_cs:
        return env_cs.strip()
    db = (config or load_config()).get("database", {})
    driver = db.get("driver", "ODBC Driver 17 for SQL Server")
    server = db.get("server", "(local)")
    database = db.get("database", "FSSInvoice")
    if db.get("trusted_connection", True):
        return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
    return (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"UID={db.get('username', '')};PWD={db.get('password', '')};"
    )


def master_connection_string(config: dict | None = None) -> str:
    db = (config or load_config()).get("database", {})
    driver = db.get("driver", "ODBC Driver 17 for SQL Server")
    server = db.get("server", "(local)")
    if db.get("trusted_connection", True):
        return f"DRIVER={{{driver}}};SERVER={server};DATABASE=master;Trusted_Connection=yes;"
    return (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE=master;"
        f"UID={db.get('username', '')};PWD={db.get('password', '')};"
    )


def get_connection(config: dict | None = None):
    """SQL Server locally; Supabase Postgres or Azure SQL in the cloud."""
    url = supabase_url()
    if url:
        import psycopg2
        from sql_pg import PgConnection
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return PgConnection(psycopg2.connect(url, **pg_connect_kwargs(url)))

    if _is_vercel():
        host, user, password, database = _vercel_credentials(config)
        import pymssql
        return pymssql.connect(
            server=host,
            user=user,
            password=password,
            database=database,
        )
    pyodbc = _import_pyodbc()
    return pyodbc.connect(connection_string(config), autocommit=False)


def _split_batches(sql: str) -> list[str]:
    parts = re.split(r"\bGO\b", sql, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _run_sql_file(cursor, path: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    for batch in _split_batches(sql):
        cursor.execute(batch)


def _run_pg_file(cursor, path: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    buf: list[str] = []
    for line in text.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            buf = []
            if stmt and not all(s.strip().startswith("--") or not s.strip() for s in stmt.splitlines()):
                cursor.execute(stmt)
    tail = "\n".join(buf).strip()
    if tail:
        cursor.execute(tail)


def migrate(config: dict | None = None) -> str:
    """Create database and apply all scripts. Safe to run multiple times."""
    cfg = config or load_config()
    if supabase_configured():
        import psycopg2
        url = supabase_url()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = psycopg2.connect(url, **pg_connect_kwargs(url))
        raw.autocommit = True
        cur = raw.cursor()
        pg_dir = os.path.join(SQL_DIR, "postgres")
        for name in ("schema.sql", "views.sql"):
            _run_pg_file(cur, os.path.join(pg_dir, name))
        raw.close()
        return "Supabase / PostgreSQL schema applied."

    if _is_vercel() and not vercel_db_configured():
        return "Skipped migration on Vercel (no database env configured)."

    if not _is_vercel():
        pyodbc = _import_pyodbc()
        with pyodbc.connect(master_connection_string(cfg), autocommit=True) as conn:
            cur = conn.cursor()
            _run_sql_file(cur, os.path.join(SQL_DIR, "01_create_database.sql"))

    scripts = ("02_tables.sql", "03_views.sql", "04_stored_procedures.sql",
               "05_ar_extensions.sql", "06_ar_views.sql", "07_ar_stored_procedures.sql",
               "09_segments_expenses.sql", "10_segment_views.sql",
               "11_non_gst_bills.sql", "12_receipts_proforma.sql")
    with get_connection(cfg) as conn:
        cur = conn.cursor()
        for name in scripts:
            _run_sql_file(cur, os.path.join(SQL_DIR, name))
            conn.commit()
    return "Database migrated successfully."


def test_connection(config: dict | None = None) -> tuple[bool, str]:
    if use_snapshot_fallback():
        import vercel_snapshot as vs
        s = vs.stats()
        return True, (
            f"Cloud snapshot active — {s['invoices']} invoices, "
            f"{s['receipts']} receipts, {s['clients']} clients")
    try:
        with get_connection(config) as conn:
            cur = conn.cursor()
            if supabase_configured():
                cur.execute("SELECT current_database()")
            else:
                cur.execute("SELECT DB_NAME()")
            row = cur.fetchone()
            label = "Supabase" if supabase_configured() else "SQL Server"
            return True, f"Connected to {row[0]} ({label})"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

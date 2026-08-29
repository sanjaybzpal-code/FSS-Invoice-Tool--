"""Create schema on cloud PostgreSQL and copy local SQL Server data."""

from __future__ import annotations

import getpass
import os
import sys
from urllib.parse import quote_plus

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOST = "43.205.3.136"
DEFAULT_PORT = "5432"
DEFAULT_DB = "fss_invoice"


def _write_env(url: str, user: str, password: str) -> None:
    path = os.path.join(HERE, ".env")
    body = (
        f"DATABASE_URL={url}\n"
        f"PGHOST={DEFAULT_HOST}\n"
        f"PGPORT={DEFAULT_PORT}\n"
        f"PGDATABASE={DEFAULT_DB}\n"
        f"PGUSER={user}\n"
        f"PGPASSWORD={password}\n"
        "PGSSLMODE=prefer\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(f"Saved connection to {path} (not committed to Git).")


def main() -> int:
    print("FSS Invoice — PostgreSQL")
    print(f"  Host: {DEFAULT_HOST}")
    print(f"  Port: {DEFAULT_PORT}")
    print(f"  Database: {DEFAULT_DB}")
    print()
    user = (os.environ.get("PGUSER") or "").strip()
    password = os.environ.get("PGPASSWORD") or ""
    if len(sys.argv) >= 3:
        user, password = sys.argv[1], sys.argv[2]
    if not user:
        user = input("Postgres username [postgres]: ").strip() or "postgres"
    if not password:
        password = getpass.getpass("Postgres password: ")
    if not password:
        print("Password required.")
        return 1
    url = (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{DEFAULT_HOST}:{DEFAULT_PORT}/{DEFAULT_DB}?sslmode=prefer"
    )
    os.environ["DATABASE_URL"] = url
    os.environ["PGUSER"] = user
    os.environ["PGPASSWORD"] = password
    _write_env(url, user, password)

    import db
    print("Connecting and applying schema...")
    try:
        print(db.migrate())
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT current_database()")
            print("Connected to:", cur.fetchone()[0])
    except Exception as exc:
        print("PostgreSQL connect/schema failed:", exc)
        print("Check username/password, and that 5432 is open from this PC.")
        return 1

    print("Copying office SQL Server data...")
    import sync_local_to_supabase as sync
    return sync.main()


if __name__ == "__main__":
    sys.exit(main())

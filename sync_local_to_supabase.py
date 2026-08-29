"""Copy local SQL Server data into Supabase (PostgreSQL).

  set SUPABASE_DB_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-...pooler.supabase.com:6543/postgres
  python sync_local_to_supabase.py
"""

from __future__ import annotations

import os
import sys

TABLES = [
    ("BusinessSegments", "businesssegments", True),
    ("ClientMaster", "clientmaster", True),
    ("TaxInvoices", "taxinvoices", True),
    ("InvoiceLineItems", "invoicelineitems", True),
    ("Receipts", "receipts", True),
    ("LedgerSequence", "ledgersequence", False),
    ("NonGstBills", "nongstbills", True),
    ("ReceiptInvoiceAllocations", "receiptinvoiceallocations", True),
    ("ReceiptNonGstAllocations", "receiptnongstallocations", True),
    ("ExpenseCategories", "expensecategories", True),
    ("Expenses", "expenses", True),
    ("ExpenseSegmentAllocations", "expensesegmentallocations", True),
    ("ReminderHistory", "reminderhistory", True),
    ("WhatsAppLog", "whatsapplog", True),
    ("ProjectCosts", "projectcosts", True),
    ("AuditLog", "auditlog", True),
    ("BankImportLog", "bankimportlog", True),
]


def _local_connect():
    saved = {}
    for key in ("SUPABASE_DB_URL", "DATABASE_URL", "POSTGRES_URL"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    import db
    try:
        return db.get_connection(), saved
    except Exception:
        for k, v in saved.items():
            os.environ[k] = v
        raise


def main() -> int:
    url = (
        os.environ.get("SUPABASE_DB_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not url:
        print("Set SUPABASE_DB_URL to your Supabase URI (Settings → Database → URI).")
        return 1

    print("Connecting to local SQL Server...")
    local, saved = _local_connect()
    try:
        lc = local.cursor()
        print("Applying Supabase schema...")
        for k, v in saved.items():
            os.environ[k] = v
        import db
        print(db.migrate())
        import psycopg2
        pg_url = url
        if pg_url.startswith("postgres://"):
            pg_url = "postgresql://" + pg_url[len("postgres://"):]
        kw = {"sslmode": "require"} if "supabase.co" in pg_url.lower() else {}
        pg = psycopg2.connect(pg_url, **kw)
        pc = pg.cursor()
        total = 0
        for mssql, pgname, identity in TABLES:
            try:
                lc.execute(
                    """SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                       WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME=?""",
                    mssql,
                )
                if not lc.fetchone():
                    continue
                lc.execute(f"SELECT * FROM dbo.{mssql}")
                cols = [c[0] for c in lc.description]
                rows = lc.fetchall()
                if not rows:
                    print(f"  {mssql}: 0 rows")
                    continue
                pc.execute(f"DELETE FROM {pgname}")
                col_list = ", ".join(c.lower() for c in cols)
                # Skip generated columns on expenses
                if pgname == "expenses":
                    skip = {"totalamount"}
                    idx = [i for i, c in enumerate(cols) if c.lower() not in skip]
                    cols = [cols[i] for i in idx]
                    rows = [tuple(r[i] for i in idx) for r in rows]
                    col_list = ", ".join(c.lower() for c in cols)
                ph = ", ".join(["%s"] * len(cols))
                ins = f"INSERT INTO {pgname} ({col_list}) VALUES ({ph})"
                for row in rows:
                    pc.execute(ins, tuple(row))
                pg.commit()
                print(f"  {mssql}: {len(rows)} rows")
                total += len(rows)
            except Exception as exc:  # noqa: BLE001
                pg.rollback()
                print(f"  {mssql}: ERROR {exc}")
        for tbl, col in (
            ("clientmaster", "clientid"),
            ("businesssegments", "businesssegmentid"),
            ("taxinvoices", "invoiceid"),
            ("invoicelineitems", "lineid"),
            ("receipts", "receiptid"),
            ("nongstbills", "nongstbillid"),
            ("receiptinvoiceallocations", "allocationid"),
            ("expensecategories", "expensecategoryid"),
            ("expenses", "expenseid"),
            ("auditlog", "auditid"),
        ):
            try:
                pc.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                    f"COALESCE((SELECT MAX({col}) FROM {tbl}), 1))",
                    (tbl, col),
                )
            except Exception:
                pass
        pg.commit()
        pc.close()
        pg.close()
        print(f"Done — {total} rows copied to Supabase.")
        print("Add SUPABASE_DB_URL on Vercel, then Redeploy.")
        return 0
    finally:
        try:
            local.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

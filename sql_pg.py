"""PostgreSQL / Supabase adapter — translates SQL Server-style queries."""

from __future__ import annotations

import re
from datetime import date

# Map lowercase PG columns back to the PascalCase names used in Python/templates.
_CANON = {n.lower(): n for n in """
ClientId ClientName GSTIN Address ContactPerson Email Mobile MhState OpeningBalance
IsActive CreatedAt UpdatedAt BusinessSegmentId BusinessSegmentName SegmentCode SortOrder
InvoiceId InvoiceNumber InvoiceDate TaxableAmount CGSTAmount SGSTAmount IGSTAmount
TotalAmount SupplyType PdfPath ExcelPath CreatedBy DueDate PaymentTermsDays InvoiceType
ConvertedFromInvoiceId LineId SrNo Particulars WorkDate Amount ReceiptId ReceiptNumber
ReceiptDate AmountReceived PaymentMode ReferenceNumber Remarks InvoiceAmount TdsPercentage
TdsAmount FinancialYear TdsCertificateReceived TdsCertificateNo TdsCertificateDate
GstAmount GstPaidAmount GstPaidStatus TdsManual SeqName NextValue NonGstBillId BillNumber
BillDate Description AllocationId AllocatedAmount AllocPercent ExpenseCategoryId
CategoryName IsSystem ExpenseId ExpenseDate ExpenseDescription VendorName AllocationType
ReminderId RuleType Channel SentAt Recipient DeliveryStatus MessageBody LogId InvoiceId
MessageType SentBy Status CostId ProjectName PeriodYear PeriodMonth Revenue
ConsultancyCharges Manhours EmployeeCost TravelCost MiscellaneousCost AuditId UserName
Action EntityType EntityId Details IpAddress ImportId FileName BankDetected ImportedBy
ImportedAt RowsTotal RowsImported RowsSkipped RowsError
TotalInvoiced TotalReceived TotalOutstanding TotalTdsDeducted TotalEffectiveReceived
TotalGstOutstanding OutstandingClientCount TotalTaxInvoiced TotalNonGstBilled
TotalGstInvoiced TotalCashReceived EffectiveReceived GstOutstanding LastInvoiceDate
LastNonGstDate LastPaymentDate TxnDate VoucherNo Debit Credit SortOrder EntryType
RunningBalance Date PaidAmount PendingAmount AgeDays AgeBucket TotalRevenue ClientCount
InvoiceCount TotalCollected Outstanding Profit ProfitPercent Expense PeriodYear
PeriodMonth Collected DueToday DueThisWeek Overdue CriticalOverdue InvYear InvMonth
InvQuarter TotalGst CgstOutstanding SgstOutstanding IgstOutstanding TotalActiveClients
TotalExpenses TotalProfit TotalGstReceivable Yr Mo ChartType Y M Month Taxable CGST SGST
IGST GstOutstanding SuggestedRule DaysToDue DaysFromDue OutstandingAmount EffDue
PaidAgainstInvoice
""".split()}


def canon_name(name: str) -> str:
    if not name:
        return name
    return _CANON.get(name.lower(), name)


def _lower_sql(sql: str) -> str:
    parts = re.split(r"('(?:''|[^'])*')", sql)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
        else:
            out.append(part.lower())
    return "".join(out)


def translate_sql(sql: str, params: tuple) -> tuple[str, tuple]:
    """Convert a SQL Server-ish statement to PostgreSQL."""
    params = tuple(params)
    # Unicode N'string'
    sql = re.sub(r"N'", "'", sql)
    sql = sql.replace("SYSUTCDATETIME()", "NOW()")
    sql = sql.replace("GETDATE()", "CURRENT_DATE")
    sql = re.sub(r"(?i)ISNULL\s*\(", "COALESCE(", sql)
    sql = re.sub(r"(?i)DB_NAME\s*\(\s*\)", "current_database()", sql)

    # SELECT TOP (?) ...  → LIMIT as last placeholder
    top_ph = re.match(r"(?is)^\s*SELECT\s+TOP\s*\(\s*\?\s*\)\s+(.*)$", sql.strip())
    if top_ph and params:
        rest = top_ph.group(1).rstrip().rstrip(";")
        sql = f"SELECT {rest} LIMIT %s"
        params = params[1:] + params[:1]
    else:
        m_topn = re.match(r"(?is)^\s*SELECT\s+TOP\s+(\d+)\s+(.*)$", sql.strip())
        if m_topn:
            n, rest = m_topn.group(1), m_topn.group(2).rstrip().rstrip(";")
            sql = f"SELECT {rest} LIMIT {int(n)}"

    # OUTPUT INSERTED.Col → RETURNING col
    m_out = re.search(r"(?is)\sOUTPUT\s+INSERTED\.(\w+)\s*", sql)
    if m_out:
        col = m_out.group(1)
        sql = sql[: m_out.start()] + " " + sql[m_out.end() :]
        sql = sql.rstrip().rstrip(";") + f" RETURNING {col}"

    sql = _lower_sql(sql)
    sql = re.sub(r"\bdbo\.", "", sql)
    # T-SQL string concat in leftover + between identifiers is rare in Python SQL

    # DATEDIFF(DAY, a, b) → (b::date - a::date)
    sql = re.sub(
        r"(?i)datediff\s*\(\s*day\s*,\s*([^,]+),\s*([^)]+)\)",
        r"((\2)::date - (\1)::date)",
        sql,
    )
    sql = re.sub(
        r"(?i)dateadd\s*\(\s*day\s*,\s*([^,]+),\s*([^)]+)\)",
        r"((\2)::date + (\1) * INTERVAL '1 day')",
        sql,
    )
    sql = re.sub(r"(?i)\byear\s*\(\s*([^)]+)\)", r"EXTRACT(YEAR FROM \1)::int", sql)
    sql = re.sub(r"(?i)\bmonth\s*\(\s*([^)]+)\)", r"EXTRACT(MONTH FROM \1)::int", sql)

    qmarks = sql.count("?")
    if qmarks:
        sql = sql.replace("?", "%s")
        if len(params) != qmarks:
            # extra params ignored/truncated by caller responsibility
            pass
    return sql, params


def _next_seq(cur, seq: str, prefix: str) -> str:
    cur.execute(
        """UPDATE ledgersequence SET nextvalue = nextvalue + 1
           WHERE seqname = %s RETURNING nextvalue - 1""",
        (seq,),
    )
    row = cur.fetchone()
    n = int(row[0]) if row else 1
    return f"{prefix}{n:05d}"


def _proc_name(sql: str) -> str | None:
    m = re.search(r"(?i)exec(?:ute)?\s+(?:dbo\.)?(\w+)", sql)
    return m.group(1).lower() if m else None


class PgCursor:
    def __init__(self, raw):
        self._raw = raw
        self._rows: list = []
        self._sets: list[tuple] = []
        self._set_i = 0
        self._queued = False
        self.description = None

    def _bind_desc(self, desc):
        if not desc:
            self.description = None
            return
        self.description = [(canon_name(d[0]), *d[1:]) for d in desc]

    def _store_simple(self):
        self._queued = False
        self._bind_desc(self._raw.description)
        try:
            self._rows = list(self._raw.fetchall())
        except Exception:
            self._rows = []

    def execute(self, sql, *params):
        if len(params) == 1 and isinstance(params[0], (list, tuple)) and not isinstance(params[0], (bytes, str)):
            # Could be execute(sql, (a,b)) or execute(sql, a) where a is tuple of values
            # pyodbc uses execute(sql, a, b, c) always in this codebase
            pass
        args = params
        proc = _proc_name(sql)
        if proc:
            return self._exec_proc(proc, args)

        pg_sql, pg_args = translate_sql(sql, args)
        if pg_args:
            self._raw.execute(pg_sql, pg_args)
        else:
            self._raw.execute(pg_sql)
        self._store_simple()
        return self

    def _exec_proc(self, proc: str, args: tuple):
        raw = self._raw
        if proc in ("sp_nextreceiptnumber",):
            num = _next_seq(raw, "RECEIPT", "RCP-")
            self.description = [("NextNumber",)]
            self._rows = [(num,)]
            self._queued = False
            return self
        if proc == "sp_nextnongstbillnumber":
            num = _next_seq(raw, "NONGST", "NGB-")
            self.description = [("NextNumber",)]
            self._rows = [(num,)]
            return self
        if proc == "sp_nextproformanumber":
            num = _next_seq(raw, "PROFORMA", "PF-")
            self.description = [("NextNumber",)]
            self._rows = [(num,)]
            return self
        if proc == "sp_logaudit":
            raw.execute(
                """INSERT INTO auditlog (username, action, entitytype, entityid, details, ipaddress)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                args if len(args) >= 6 else args + (None,) * (6 - len(args)),
            )
            self.description = None
            self._rows = []
            return self
        if proc == "sp_getoutstandingdashboard":
            raw.execute("SELECT * FROM vw_outstandingdashboard")
            self._store_simple()
            return self
        if proc == "sp_getclientsummary":
            raw.execute("SELECT * FROM vw_clientoutstanding WHERE clientid = %s", args[:1])
            self._store_simple()
            return self
        if proc == "sp_getageinganalysis":
            raw.execute(
                """SELECT agebucket, COUNT(*) AS invoicecount, SUM(pendingamount) AS pendingamount
                   FROM vw_invoiceageing GROUP BY agebucket
                   ORDER BY CASE agebucket
                     WHEN '0-30 Days' THEN 1 WHEN '31-60 Days' THEN 2
                     WHEN '61-90 Days' THEN 3 ELSE 4 END"""
            )
            self._store_simple()
            return self
        if proc == "sp_getclientledger":
            cid = args[0] if args else None
            fd = args[1] if len(args) > 1 else None
            td = args[2] if len(args) > 2 else None
            seg = args[3] if len(args) > 3 else None
            fd = fd or date(1900, 1, 1)
            td = td or date(9999, 12, 31)
            raw.execute(
                """
                WITH ledger AS (
                    SELECT l.txndate, l.voucherno, l.particulars, l.debit, l.credit, l.sortorder
                    FROM vw_clientledger l
                    WHERE l.clientid = %s AND l.txndate BETWEEN %s AND %s
                      AND (%s IS NULL OR l.sortorder = 0
                           OR EXISTS (SELECT 1 FROM taxinvoices ti
                                      WHERE ti.invoicenumber = l.voucherno AND ti.businesssegmentid = %s)
                           OR EXISTS (SELECT 1 FROM nongstbills nb
                                      WHERE nb.billnumber = l.voucherno AND nb.businesssegmentid = %s)
                           OR EXISTS (
                               SELECT 1 FROM receipts r
                               INNER JOIN receiptinvoiceallocations ria ON ria.receiptid = r.receiptid
                               WHERE r.receiptnumber = l.voucherno AND ria.businesssegmentid = %s))
                ),
                running AS (
                    SELECT txndate, voucherno, particulars, debit, credit,
                           SUM(debit - credit) OVER (ORDER BY txndate, sortorder, voucherno
                               ROWS UNBOUNDED PRECEDING) AS runningbalance
                    FROM ledger
                )
                SELECT txndate AS date, voucherno, particulars, debit, credit, runningbalance
                FROM running ORDER BY txndate, voucherno
                """,
                (cid, fd, td, seg, seg, seg, seg),
            )
            self._store_simple()
            return self
        if proc == "sp_getreminderdashboard":
            raw.execute("SELECT * FROM vw_reminderdashboard")
            self._store_simple()
            return self
        if proc == "sp_getremindersdue":
            raw.execute(
                """SELECT i.invoiceid, i.clientid, c.clientname, c.email, c.mobile,
                          i.invoicenumber, i.invoicedate,
                          COALESCE(i.duedate, i.invoicedate + 30) AS effdue,
                          ia.pendingamount AS outstandingamount, i.pdfpath,
                          (COALESCE(i.duedate, i.invoicedate + 30) - CURRENT_DATE) AS daystodue
                   FROM taxinvoices i
                   INNER JOIN clientmaster c ON c.clientid = i.clientid
                   INNER JOIN vw_invoiceageing ia ON ia.invoiceid = i.invoiceid
                   WHERE ia.pendingamount > 0.01"""
            )
            self._store_simple()
            return self
        if proc == "sp_getgstmonthwise":
            year = args[0] if args else date.today().year
            raw.execute(
                """SELECT invmonth AS month, SUM(taxableamount) AS taxable,
                          SUM(cgstamount) AS cgst, SUM(sgstamount) AS sgst, SUM(igstamount) AS igst,
                          SUM(cgstoutstanding + sgstoutstanding + igstoutstanding) AS gstoutstanding
                   FROM vw_gstreceivable WHERE invyear = %s
                   GROUP BY invmonth ORDER BY invmonth""",
                (year,),
            )
            self._store_simple()
            return self
        if proc == "sp_getgstclientwise":
            raw.execute(
                """SELECT clientname, SUM(taxableamount) AS taxable,
                          SUM(cgstamount) AS cgst, SUM(sgstamount) AS sgst, SUM(igstamount) AS igst,
                          SUM(cgstoutstanding + sgstoutstanding + igstoutstanding) AS gstoutstanding
                   FROM vw_gstreceivable GROUP BY clientname ORDER BY gstoutstanding DESC"""
            )
            self._store_simple()
            return self
        if proc == "sp_getsegmentdashboard":
            raw.execute("SELECT * FROM vw_segmentoutstanding ORDER BY businesssegmentid")
            self._store_simple()
            return self
        if proc == "sp_getsegmentprofitability":
            raw.execute("SELECT * FROM vw_segmentprofitability ORDER BY businesssegmentid")
            self._store_simple()
            return self
        if proc == "sp_getsegmentmonthlytrend":
            months = int(args[0]) if args else 12
            raw.execute(
                """
                WITH months AS (
                    SELECT EXTRACT(YEAR FROM d)::int AS y, EXTRACT(MONTH FROM d)::int AS m
                    FROM generate_series(
                        date_trunc('month', CURRENT_DATE) - (%s - 1) * INTERVAL '1 month',
                        date_trunc('month', CURRENT_DATE),
                        INTERVAL '1 month') AS d
                )
                SELECT s.businesssegmentid, s.businesssegmentname, mo.y AS periodyear, mo.m AS periodmonth,
                       COALESCE(r.revenue, 0) AS revenue, COALESCE(e.expense, 0) AS expense,
                       COALESCE(r.revenue, 0) - COALESCE(e.expense, 0) AS profit,
                       COALESCE(col.collected, 0) AS collected,
                       COALESCE(r.revenue, 0) - COALESCE(col.collected, 0) AS outstanding
                FROM businesssegments s
                CROSS JOIN months mo
                LEFT JOIN vw_segmentmonthlyrevenue r
                    ON r.businesssegmentid = s.businesssegmentid AND r.periodyear = mo.y AND r.periodmonth = mo.m
                LEFT JOIN vw_segmentmonthlyexpense e
                    ON e.businesssegmentid = s.businesssegmentid AND e.periodyear = mo.y AND e.periodmonth = mo.m
                LEFT JOIN (
                    SELECT a.businesssegmentid, EXTRACT(YEAR FROM r2.receiptdate)::int AS y,
                           EXTRACT(MONTH FROM r2.receiptdate)::int AS m, SUM(a.allocatedamount) AS collected
                    FROM receiptinvoiceallocations a
                    INNER JOIN receipts r2 ON r2.receiptid = a.receiptid
                    GROUP BY a.businesssegmentid, EXTRACT(YEAR FROM r2.receiptdate), EXTRACT(MONTH FROM r2.receiptdate)
                ) col ON col.businesssegmentid = s.businesssegmentid AND col.y = mo.y AND col.m = mo.m
                WHERE s.isactive = 1
                ORDER BY mo.y, mo.m, s.sortorder
                """,
                (months,),
            )
            self._store_simple()
            return self
        if proc == "sp_getexecutivecharts":
            queries = [
                """SELECT EXTRACT(YEAR FROM invoicedate)::int AS yr, EXTRACT(MONTH FROM invoicedate)::int AS mo,
                          SUM(totalamount) AS revenue FROM taxinvoices
                   WHERE COALESCE(invoicetype,'TAX') <> 'PROFORMA'
                   GROUP BY 1, 2 ORDER BY 1, 2""",
                """SELECT EXTRACT(YEAR FROM receiptdate)::int AS yr, EXTRACT(MONTH FROM receiptdate)::int AS mo,
                          SUM(amountreceived) AS collections FROM receipts GROUP BY 1, 2 ORDER BY 1, 2""",
                """SELECT c.clientname, SUM(i.totalamount) AS revenue FROM taxinvoices i
                   INNER JOIN clientmaster c ON c.clientid = i.clientid
                   WHERE COALESCE(i.invoicetype,'TAX') <> 'PROFORMA'
                   GROUP BY c.clientname ORDER BY revenue DESC LIMIT 10""",
                """SELECT clientname, outstanding FROM vw_clientoutstanding
                   WHERE outstanding > 0 ORDER BY outstanding DESC LIMIT 10""",
                """SELECT businesssegmentid, businesssegmentname, periodyear AS y, periodmonth AS m, revenue AS amount
                   FROM vw_segmentmonthlyrevenue ORDER BY y, m, businesssegmentid""",
                """SELECT businesssegmentid, businesssegmentname, periodyear AS y, periodmonth AS m, expense AS amount
                   FROM vw_segmentmonthlyexpense ORDER BY y, m, businesssegmentid""",
            ]
            self._sets = []
            for q in queries:
                raw.execute(q)
                desc = raw.description
                rows = list(raw.fetchall())
                self._sets.append((desc, rows))
            self._queued = True
            self._set_i = 0
            self._bind_desc(self._sets[0][0])
            self._rows = self._sets[0][1]
            return self

        # Unknown proc: try as translated SQL
        pg_sql, pg_args = translate_sql(sql, args)
        if pg_args:
            raw.execute(pg_sql, pg_args)
        else:
            raw.execute(pg_sql)
        self._store_simple()
        return self

    def nextset(self):
        if not self._queued:
            return False
        if self._set_i + 1 >= len(self._sets):
            return False
        self._set_i += 1
        desc, rows = self._sets[self._set_i]
        self._bind_desc(desc)
        self._rows = rows
        return True

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return rows

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def close(self):
        self._raw.close()


class PgConnection:
    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return PgCursor(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False

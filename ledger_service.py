"""Client ledger, receipts, and reporting — SQL Server data layer."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import db


def _snap():
    if db.use_snapshot_fallback():
        import vercel_snapshot as vs
        return vs
    return None


def _rows(cursor) -> list[dict]:
    cols = [c[0] for c in cursor.description] if cursor.description else []
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _row(cursor) -> dict | None:
    rows = _rows(cursor)
    return rows[0] if rows else None


def _parse_date(value: str | date | None) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    s = s[:10]
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --- Client Master -----------------------------------------------------------
def upsert_client(name: str, gstin: str = "", address: str = "",
                  contact: str = "", email: str = "", mobile: str = "",
                  mh: bool = False) -> int:
    s = _snap()
    if s:
        return s.upsert_client(name, gstin, address, contact, email, mobile, mh)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ClientId FROM dbo.ClientMaster WHERE ClientName = ?", name)
        row = cur.fetchone()
        if row:
            client_id = int(row[0])
            cur.execute(
                """UPDATE dbo.ClientMaster SET
                   GSTIN=COALESCE(?,GSTIN), Address=COALESCE(?,Address),
                   ContactPerson=COALESCE(?,ContactPerson), Email=COALESCE(?,Email),
                   Mobile=COALESCE(?,Mobile), MhState=?, UpdatedAt=SYSUTCDATETIME()
                   WHERE ClientId=?""",
                gstin or None, address or None, contact or None,
                email or None, mobile or None, 1 if mh else 0, client_id)
        else:
            cur.execute(
                """INSERT INTO dbo.ClientMaster
                   (ClientName,GSTIN,Address,ContactPerson,Email,Mobile,MhState)
                   OUTPUT INSERTED.ClientId VALUES (?,?,?,?,?,?,?)""",
                name, gstin or None, address or None, contact or None,
                email or None, mobile or None, 1 if mh else 0)
            client_id = int(cur.fetchone()[0])
        conn.commit()
        return client_id


def list_clients(active_only: bool = True) -> list[dict]:
    s = _snap()
    if s:
        return s.list_clients(active_only)
    with db.get_connection() as conn:
        cur = conn.cursor()
        sql = "SELECT * FROM dbo.ClientMaster"
        if active_only:
            sql += " WHERE IsActive = 1"
        sql += " ORDER BY ClientName"
        cur.execute(sql)
        return _rows(cur)


def get_client(client_id: int) -> dict | None:
    s = _snap()
    if s:
        return s.get_client(client_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dbo.ClientMaster WHERE ClientId = ?", client_id)
        return _row(cur)


def update_client(client_id: int, **fields) -> None:
    allowed = {"ClientName", "GSTIN", "Address", "ContactPerson",
               "Email", "Mobile", "MhState", "OpeningBalance", "IsActive"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    if "ClientName" in fields:
        new_name = (fields["ClientName"] or "").strip()
        if not new_name:
            raise ValueError("Client name cannot be empty.")
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT 1 FROM dbo.ClientMaster
                   WHERE ClientName = ? AND ClientId <> ?""",
                new_name, client_id)
            if cur.fetchone():
                raise ValueError(f"Client name '{new_name}' is already in use.")
    vals.append(client_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE dbo.ClientMaster SET {', '.join(sets)}, UpdatedAt = SYSUTCDATETIME() "
            f"WHERE ClientId = ?", *vals)
        conn.commit()


def client_usage(client_id: int) -> dict:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dbo.TaxInvoices WHERE ClientId = ?", client_id)
        inv = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM dbo.Receipts WHERE ClientId = ?", client_id)
        rcp = int(cur.fetchone()[0])
        return {"invoices": inv, "receipts": rcp}


def create_client(name: str, gstin: str = "", address: str = "",
                  contact: str = "", email: str = "", mobile: str = "",
                  mh: bool = False, opening_balance: float = 0) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Client name is required.")
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM dbo.ClientMaster WHERE ClientName = ?", name)
        if cur.fetchone():
            raise ValueError(f"Client '{name}' already exists.")
        cur.execute(
            """INSERT INTO dbo.ClientMaster
               (ClientName, GSTIN, Address, ContactPerson, Email, Mobile, MhState, OpeningBalance)
               OUTPUT INSERTED.ClientId VALUES (?,?,?,?,?,?,?,?)""",
            name, gstin or None, address or None, contact or None,
            email or None, mobile or None, 1 if mh else 0, float(opening_balance or 0))
        client_id = int(cur.fetchone()[0])
        conn.commit()
        return client_id


def delete_client(client_id: int) -> tuple[bool, str]:
    client = get_client(client_id)
    if not client:
        return False, "Client not found."
    usage = client_usage(client_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        if usage["invoices"] or usage["receipts"]:
            cur.execute(
                """UPDATE dbo.ClientMaster SET IsActive = 0, UpdatedAt = SYSUTCDATETIME()
                   WHERE ClientId = ?""",
                client_id)
            conn.commit()
            return True, (
                f"Client '{client['ClientName']}' deactivated — "
                f"{usage['invoices']} invoice(s) and {usage['receipts']} receipt(s) kept on record."
            )
        cur.execute("DELETE FROM dbo.ClientMaster WHERE ClientId = ?", client_id)
        conn.commit()
        return True, f"Client '{client['ClientName']}' deleted permanently."


# --- Tax Invoices (auto debit) -----------------------------------------------
def record_tax_invoice(client_id: int, invoice_number: str, invoice_date: str,
                       taxable: float, cgst: float, sgst: float, igst: float,
                       total: float, supply_type: str, line_items: list[dict],
                       pdf_path: str = "", excel_path: str = "",
                       created_by: str = "", segment_id: int = 1,
                       invoice_type: str = "TAX") -> int:
    s = _snap()
    if s:
        return s.record_tax_invoice(
            client_id, invoice_number, invoice_date, taxable, cgst, sgst, igst,
            total, supply_type, line_items, pdf_path, excel_path, created_by,
            segment_id, invoice_type)
    existing = find_invoice_by_number(str(invoice_number))
    if existing:
        update_invoice(
            int(existing["InvoiceId"]),
            segment_id=segment_id,
            invoice_date=invoice_date,
            client_id=client_id,
            taxable_amount=taxable,
            cgst_amount=cgst,
            sgst_amount=sgst,
            igst_amount=igst,
            total_amount=total,
            supply_type=supply_type,
            line_items=line_items,
            invoice_type=(invoice_type or existing.get("InvoiceType") or "TAX"),
            pdf_path=pdf_path or None,
            excel_path=excel_path or None,
        )
        return int(existing["InvoiceId"])
    inv_date = _parse_date(invoice_date) or date.today()
    terms = 30
    try:
        import json
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            terms = int(json.load(fh).get("reminders", {}).get("payment_terms_days", 30))
    except (OSError, ValueError, TypeError):
        pass
    from datetime import timedelta
    due_date = inv_date + timedelta(days=terms)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO dbo.TaxInvoices
               (ClientId, InvoiceNumber, InvoiceDate, DueDate, PaymentTermsDays,
                TaxableAmount, CGSTAmount, SGSTAmount, IGSTAmount, TotalAmount,
                SupplyType, PdfPath, ExcelPath, CreatedBy, BusinessSegmentId, InvoiceType)
               OUTPUT INSERTED.InvoiceId
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            client_id, str(invoice_number), inv_date, due_date, terms, taxable,
            cgst, sgst, igst, total, supply_type, pdf_path or None,
            excel_path or None, created_by or None, segment_id or 1,
            (invoice_type or "TAX").upper())
        invoice_id = int(cur.fetchone()[0])
        for i, it in enumerate(line_items, 1):
            wd = _parse_date(it.get("date"))
            cur.execute(
                """INSERT INTO dbo.InvoiceLineItems
                   (InvoiceId, SrNo, Particulars, WorkDate, Amount)
                   VALUES (?,?,?,?,?)""",
                invoice_id, i, it["particulars"], wd, float(it["amount"]))
        conn.commit()
        return invoice_id


# --- Non-GST Bills (without tax — separate from tax invoices) ----------------
def next_non_gst_bill_number() -> str:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DECLARE @n NVARCHAR(30); EXEC dbo.sp_NextNonGstBillNumber @n OUTPUT; SELECT @n")
        num = str(cur.fetchone()[0])
        conn.commit()
        return num


def peek_non_gst_bill_number() -> str:
    s = _snap()
    if s:
        return s.peek_non_gst_bill_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT NextValue FROM dbo.LedgerSequence WHERE SeqName = N'NONGST'")
        row = cur.fetchone()
        n = int(row[0]) if row else 1
        return f"NGB-{n:05d}"


def create_non_gst_bill(client_id: int, bill_date: str, amount: float,
                        description: str, segment_id: int = 1,
                        remarks: str = "", bill_number: str | None = None,
                        created_by: str = "") -> int:
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    desc = (description or "").strip()
    if not desc:
        raise ValueError("Description is required.")
    bdate = _parse_date(bill_date) or date.today()
    bnum = bill_number or next_non_gst_bill_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO dbo.NonGstBills
               (ClientId, BillNumber, BillDate, Amount, Description,
                BusinessSegmentId, Remarks, CreatedBy)
               OUTPUT INSERTED.NonGstBillId
               VALUES (?,?,?,?,?,?,?,?)""",
            client_id, bnum, bdate, amount, desc, segment_id or 1,
            remarks or None, created_by or None)
        bill_id = int(cur.fetchone()[0])
        conn.commit()
        return bill_id


def list_non_gst_bills(client_id: int | None = None, limit: int = 500,
                       segment_id: int | None = None) -> list[dict]:
    s = _snap()
    if s:
        return s.list_non_gst_bills(client_id, limit, segment_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        sql = """SELECT TOP (?) n.*, c.ClientName, s.BusinessSegmentName
                 FROM dbo.NonGstBills n
                 INNER JOIN dbo.ClientMaster c ON c.ClientId = n.ClientId
                 LEFT JOIN dbo.BusinessSegments s ON s.BusinessSegmentId = n.BusinessSegmentId
                 WHERE 1=1"""
        params: list = [limit]
        if client_id:
            sql += " AND n.ClientId = ?"
            params.append(client_id)
        if segment_id:
            sql += " AND n.BusinessSegmentId = ?"
            params.append(segment_id)
        sql += " ORDER BY n.BillDate DESC, n.NonGstBillId DESC"
        cur.execute(sql, *params)
        return _rows(cur)


def get_non_gst_bill(bill_id: int) -> dict | None:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT n.*, c.ClientName, s.BusinessSegmentName
               FROM dbo.NonGstBills n
               INNER JOIN dbo.ClientMaster c ON c.ClientId = n.ClientId
               LEFT JOIN dbo.BusinessSegments s ON s.BusinessSegmentId = n.BusinessSegmentId
               WHERE n.NonGstBillId = ?""", bill_id)
        return _row(cur)


def update_non_gst_bill(bill_id: int, amount: float | None = None,
                        description: str | None = None,
                        bill_date: str | None = None,
                        segment_id: int | None = None,
                        remarks: str | None = None) -> None:
    fields, vals = [], []
    if amount is not None:
        if amount <= 0:
            raise ValueError("Amount must be positive.")
        fields.append("Amount = ?")
        vals.append(amount)
    if description is not None:
        fields.append("Description = ?")
        vals.append(description.strip())
    if bill_date:
        fields.append("BillDate = ?")
        vals.append(_parse_date(bill_date))
    if segment_id is not None:
        fields.append("BusinessSegmentId = ?")
        vals.append(segment_id)
    if remarks is not None:
        fields.append("Remarks = ?")
        vals.append(remarks or None)
    if not fields:
        return
    vals.append(bill_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE dbo.NonGstBills SET {', '.join(fields)} WHERE NonGstBillId = ?",
            *vals)
        if segment_id is not None:
            cur.execute(
                """UPDATE dbo.ReceiptNonGstAllocations SET BusinessSegmentId = ?
                   WHERE NonGstBillId = ?""", segment_id, bill_id)
        conn.commit()


def delete_non_gst_bill(bill_id: int) -> tuple[bool, str]:
    bill = get_non_gst_bill(bill_id)
    if not bill:
        return False, "Bill not found."
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ISNULL(SUM(AllocatedAmount),0) FROM dbo.ReceiptNonGstAllocations WHERE NonGstBillId=?",
            bill_id)
        paid = float(cur.fetchone()[0])
        if paid > 0.01:
            return False, f"Cannot delete — ₹{paid:,.2f} already received against this bill."
        cur.execute("DELETE FROM dbo.NonGstBills WHERE NonGstBillId = ?", bill_id)
        conn.commit()
    return True, f"Non-GST bill {bill['BillNumber']} deleted."


def next_proforma_number() -> str:
    s = _snap()
    if s:
        return s.next_proforma_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DECLARE @n NVARCHAR(30); EXEC dbo.sp_NextProformaNumber @n OUTPUT; SELECT @n")
        num = str(cur.fetchone()[0])
        conn.commit()
        return num


def peek_proforma_number() -> str:
    s = _snap()
    if s:
        return s.peek_proforma_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT NextValue FROM dbo.LedgerSequence WHERE SeqName = N'PROFORMA'")
        row = cur.fetchone()
        n = int(row[0]) if row else 1
        return f"PF-{n:05d}"


def max_tax_invoice_number() -> int:
    """Highest numeric tax invoice number (used so cloud numbering does not collide)."""
    s = _snap()
    if s:
        return s.max_tax_invoice_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT InvoiceNumber FROM dbo.TaxInvoices
               WHERE ISNULL(InvoiceType, N'TAX') <> N'PROFORMA'""")
        n = 0
        for (num,) in cur.fetchall():
            try:
                n = max(n, int(str(num)))
            except (TypeError, ValueError):
                continue
        return n


def find_invoice_by_number(invoice_number: str) -> dict | None:
    num = str(invoice_number or "").strip()
    if not num:
        return None
    s = _snap()
    if s:
        return s.find_invoice_by_number(num)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT i.*, c.ClientName, s.BusinessSegmentName
               FROM dbo.TaxInvoices i
               INNER JOIN dbo.ClientMaster c ON c.ClientId = i.ClientId
               LEFT JOIN dbo.BusinessSegments s ON s.BusinessSegmentId = i.BusinessSegmentId
               WHERE i.InvoiceNumber = ?""",
            num)
        return _row(cur)


def list_invoices(client_id: int | None = None, limit: int = 5000,
                  segment_id: int | None = None,
                  invoice_type: str | None = None) -> list[dict]:
    s = _snap()
    if s:
        return s.list_invoices(client_id, limit, segment_id, invoice_type)
    with db.get_connection() as conn:
        cur = conn.cursor()
        sql = """SELECT TOP (?) i.*, c.ClientName, s.BusinessSegmentName
                 FROM dbo.TaxInvoices i
                 INNER JOIN dbo.ClientMaster c ON c.ClientId = i.ClientId
                 LEFT JOIN dbo.BusinessSegments s ON s.BusinessSegmentId = i.BusinessSegmentId
                 WHERE 1=1"""
        params: list = [limit]
        if client_id:
            sql += " AND i.ClientId = ?"
            params.append(client_id)
        if segment_id:
            sql += " AND i.BusinessSegmentId = ?"
            params.append(segment_id)
        if invoice_type:
            sql += " AND ISNULL(i.InvoiceType, N'TAX') = ?"
            params.append(invoice_type.upper())
        sql += " ORDER BY i.InvoiceDate DESC, i.InvoiceId DESC"
        cur.execute(sql, *params)
        return _rows(cur)


def get_invoice(invoice_id: int) -> dict | None:
    s = _snap()
    if s:
        return s.get_invoice(invoice_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT i.*, c.ClientName, s.BusinessSegmentName
               FROM dbo.TaxInvoices i
               INNER JOIN dbo.ClientMaster c ON c.ClientId = i.ClientId
               LEFT JOIN dbo.BusinessSegments s ON s.BusinessSegmentId = i.BusinessSegmentId
               WHERE i.InvoiceId = ?""",
            invoice_id)
        return _row(cur)


def get_invoice_line_items(invoice_id: int) -> list[dict]:
    """Return all line items for an invoice, ordered by SrNo."""
    s = _snap()
    if s:
        return s.get_invoice_line_items(invoice_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT LineId, SrNo, Particulars, WorkDate, Amount
               FROM dbo.InvoiceLineItems
               WHERE InvoiceId = ?
               ORDER BY SrNo""",
            invoice_id)
        return _rows(cur)


def _resolve_stored_path(stored: str | None, output_folder: str) -> str | None:
    """Find invoice file on disk from DB path or archive folder."""
    if not stored:
        return None
    stored = stored.strip()
    if os.path.isfile(stored):
        return stored
    base = os.path.basename(stored)
    in_folder = os.path.join(output_folder, base)
    if os.path.isfile(in_folder):
        return in_folder
    return None


def invoice_download_paths(invoice: dict, output_folder: str) -> dict[str, str | None]:
    """Return {'pdf': path, 'excel': path} for an invoice row."""
    return {
        "pdf": _resolve_stored_path(invoice.get("PdfPath"), output_folder),
        "excel": _resolve_stored_path(invoice.get("ExcelPath"), output_folder),
    }


def update_invoice(invoice_id: int, segment_id: int | None = None,
                   invoice_date: str | None = None,
                   client_id: int | None = None,
                   taxable_amount: float | None = None,
                   cgst_amount: float | None = None,
                   sgst_amount: float | None = None,
                   igst_amount: float | None = None,
                   total_amount: float | None = None,
                   supply_type: str | None = None,
                   line_items: list[dict] | None = None,
                   invoice_number: str | None = None,
                   invoice_type: str | None = None,
                   pdf_path: str | None = None,
                   excel_path: str | None = None) -> None:
    """Full invoice edit — update any combination of fields and replace line items.

    If line_items is provided, all existing line items are deleted and replaced.
    Receipt allocations are updated when segment or total changes.
    """
    s = _snap()
    if s:
        s.update_invoice(
            invoice_id, segment_id=segment_id, invoice_date=invoice_date,
            client_id=client_id, taxable_amount=taxable_amount,
            cgst_amount=cgst_amount, sgst_amount=sgst_amount,
            igst_amount=igst_amount, total_amount=total_amount,
            supply_type=supply_type, line_items=line_items,
            invoice_number=invoice_number, invoice_type=invoice_type,
            pdf_path=pdf_path, excel_path=excel_path)
        return
    inv_date = _parse_date(invoice_date) if invoice_date else None
    sets, vals = [], []
    if invoice_number is not None:
        new_no = str(invoice_number).strip()
        if not new_no:
            raise ValueError("Invoice number cannot be empty.")
        other = find_invoice_by_number(new_no)
        if other and int(other["InvoiceId"]) != int(invoice_id):
            raise ValueError(f"Invoice number {new_no} is already used.")
        sets.append("InvoiceNumber = ?")
        vals.append(new_no)
    if segment_id is not None:
        sets.append("BusinessSegmentId = ?")
        vals.append(segment_id)
    if inv_date is not None:
        sets.append("InvoiceDate = ?")
        vals.append(inv_date)
        current = get_invoice(invoice_id) or {}
        terms = int(current.get("PaymentTermsDays") or 30)
        from datetime import timedelta
        sets.append("DueDate = ?")
        vals.append(inv_date + timedelta(days=terms))
    if client_id is not None:
        sets.append("ClientId = ?")
        vals.append(client_id)
    if taxable_amount is not None:
        sets.append("TaxableAmount = ?")
        vals.append(taxable_amount)
    if cgst_amount is not None:
        sets.append("CGSTAmount = ?")
        vals.append(cgst_amount)
    if sgst_amount is not None:
        sets.append("SGSTAmount = ?")
        vals.append(sgst_amount)
    if igst_amount is not None:
        sets.append("IGSTAmount = ?")
        vals.append(igst_amount)
    if total_amount is not None:
        sets.append("TotalAmount = ?")
        vals.append(total_amount)
    if supply_type is not None:
        sets.append("SupplyType = ?")
        vals.append(supply_type)
    if invoice_type is not None:
        sets.append("InvoiceType = ?")
        vals.append(str(invoice_type).upper())
    if pdf_path is not None:
        sets.append("PdfPath = ?")
        vals.append(pdf_path or None)
    if excel_path is not None:
        sets.append("ExcelPath = ?")
        vals.append(excel_path or None)

    with db.get_connection() as conn:
        cur = conn.cursor()
        if sets:
            vals.append(invoice_id)
            cur.execute(
                f"UPDATE dbo.TaxInvoices SET {', '.join(sets)} WHERE InvoiceId = ?",
                *vals)
        # Keep receipt allocations in sync with segment changes
        if segment_id is not None:
            cur.execute(
                """UPDATE dbo.ReceiptInvoiceAllocations
                   SET BusinessSegmentId = ? WHERE InvoiceId = ?""",
                segment_id, invoice_id)
        # Replace line items atomically
        if line_items is not None:
            cur.execute(
                "DELETE FROM dbo.InvoiceLineItems WHERE InvoiceId = ?",
                invoice_id)
            for i, it in enumerate(line_items, 1):
                wd = _parse_date(it.get("date") or it.get("WorkDate"))
                cur.execute(
                    """INSERT INTO dbo.InvoiceLineItems
                       (InvoiceId, SrNo, Particulars, WorkDate, Amount)
                       VALUES (?,?,?,?,?)""",
                    invoice_id, i,
                    (it.get("particulars") or it.get("Particulars") or "").strip(),
                    wd,
                    float(it.get("amount") if it.get("amount") is not None else it.get("Amount") or 0))
        conn.commit()


def enrich_invoices_with_files(rows: list[dict], output_folder: str) -> list[dict]:
    """Attach has_pdf / has_excel and display filenames for archive UI."""
    out = []
    for inv in rows:
        paths = invoice_download_paths(inv, output_folder)
        inv = dict(inv)
        inv["has_pdf"] = bool(paths["pdf"])
        inv["has_excel"] = bool(paths["excel"])
        inv["pdf_name"] = os.path.basename(paths["pdf"]) if paths["pdf"] else ""
        inv["excel_name"] = os.path.basename(paths["excel"]) if paths["excel"] else ""
        out.append(inv)
    return out


# --- Receipts (credit) -------------------------------------------------------
def next_receipt_number() -> str:
    s = _snap()
    if s:
        return s.next_receipt_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DECLARE @n NVARCHAR(30); EXEC dbo.sp_NextReceiptNumber @n OUTPUT; SELECT @n")
        num = str(cur.fetchone()[0])
        conn.commit()
        return num


def add_receipt(client_id: int, receipt_date: str, amount: float,
                payment_mode: str, reference: str = "", remarks: str = "",
                receipt_number: str | None = None, created_by: str = "",
                invoice_ids: list[int] | None = None) -> str:
    rdate = _parse_date(receipt_date) or date.today()
    rnum = receipt_number or next_receipt_number()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO dbo.Receipts
               (ClientId, ReceiptNumber, ReceiptDate, AmountReceived,
                PaymentMode, ReferenceNumber, Remarks, CreatedBy)
               OUTPUT INSERTED.ReceiptId
               VALUES (?,?,?,?,?,?,?,?)""",
            client_id, rnum, rdate, amount, payment_mode,
            reference or None, remarks or None, created_by or None)
        receipt_id = int(cur.fetchone()[0])
        _allocate_receipt(cur, receipt_id, client_id, amount, invoice_ids)
        conn.commit()
        return rnum


def _pending_debits(cur, client_id: int) -> list[dict]:
    """Tax invoices + non-GST bills with pending balance, FIFO order."""
    cur.execute(
        """SELECT N'tax' AS BillType, i.InvoiceId AS BillId, i.InvoiceNumber AS BillNumber,
                  i.InvoiceDate AS BillDate, i.BusinessSegmentId,
                  i.TotalAmount - ISNULL(SUM(ria.AllocatedAmount), 0) AS Pending
           FROM dbo.TaxInvoices i
           LEFT JOIN dbo.ReceiptInvoiceAllocations ria ON ria.InvoiceId = i.InvoiceId
           WHERE i.ClientId = ?
             AND ISNULL(i.InvoiceType, N'TAX') <> N'PROFORMA'
           GROUP BY i.InvoiceId, i.InvoiceNumber, i.InvoiceDate, i.BusinessSegmentId, i.TotalAmount
           HAVING i.TotalAmount - ISNULL(SUM(ria.AllocatedAmount), 0) > 0.01
           UNION ALL
           SELECT N'nongst', n.NonGstBillId, n.BillNumber, n.BillDate, n.BusinessSegmentId,
                  n.Amount - ISNULL(SUM(rng.AllocatedAmount), 0)
           FROM dbo.NonGstBills n
           LEFT JOIN dbo.ReceiptNonGstAllocations rng ON rng.NonGstBillId = n.NonGstBillId
           WHERE n.ClientId = ?
           GROUP BY n.NonGstBillId, n.BillNumber, n.BillDate, n.BusinessSegmentId, n.Amount
           HAVING n.Amount - ISNULL(SUM(rng.AllocatedAmount), 0) > 0.01
           ORDER BY BillDate, BillId""", client_id, client_id)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _invoice_pending(cur, client_id: int,
                     exclude_receipt_id: int | None = None) -> list[dict]:
    """Tax invoices with pending balance.

    When editing a receipt, pass exclude_receipt_id so that invoice is still
    listed (pending includes this receipt's current allocation).
    """
    excl = int(exclude_receipt_id or 0)
    cur.execute(
        """SELECT i.InvoiceId, i.InvoiceNumber, i.TaxableAmount, i.TotalAmount,
                  i.BusinessSegmentId,
                  i.TotalAmount - ISNULL(SUM(CASE
                      WHEN ria.ReceiptId IS NULL OR ria.ReceiptId <> ?
                      THEN ria.AllocatedAmount ELSE 0 END), 0) AS Pending
           FROM dbo.TaxInvoices i
           LEFT JOIN dbo.ReceiptInvoiceAllocations ria ON ria.InvoiceId = i.InvoiceId
           WHERE i.ClientId = ?
             AND ISNULL(i.InvoiceType, N'TAX') <> N'PROFORMA'
           GROUP BY i.InvoiceId, i.InvoiceNumber, i.InvoiceDate, i.TaxableAmount,
                    i.TotalAmount, i.BusinessSegmentId
           HAVING i.TotalAmount - ISNULL(SUM(CASE
                      WHEN ria.ReceiptId IS NULL OR ria.ReceiptId <> ?
                      THEN ria.AllocatedAmount ELSE 0 END), 0) > 0.01
               OR MAX(CASE WHEN ria.ReceiptId = ? THEN 1 ELSE 0 END) = 1
           ORDER BY i.InvoiceDate, i.InvoiceId""",
        excl, client_id, excl, excl)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _allocate_receipt(cur, receipt_id: int, client_id: int, amount: float,
                      invoice_ids: list[int] | None = None,
                      tds_amount: float = 0) -> None:
    """Allocate receipt to tax invoices and non-GST bills (FIFO). Includes TDS in settlement."""
    remaining = float(amount) + float(tds_amount or 0)
    if invoice_ids:
        placeholders = ",".join("?" * len(invoice_ids))
        cur.execute(
            f"""SELECT N'tax' AS BillType, i.InvoiceId AS BillId, i.BusinessSegmentId,
                       i.TotalAmount - ISNULL(SUM(ria.AllocatedAmount), 0) AS Pending
                FROM dbo.TaxInvoices i
                LEFT JOIN dbo.ReceiptInvoiceAllocations ria ON ria.InvoiceId = i.InvoiceId
                WHERE i.InvoiceId IN ({placeholders})
                  AND ISNULL(i.InvoiceType, N'TAX') <> N'PROFORMA'
                GROUP BY i.InvoiceId, i.BusinessSegmentId, i.TotalAmount""",
            *invoice_ids)
        cols = [c[0] for c in cur.description]
        targets = [dict(zip(cols, row)) for row in cur.fetchall()]
        total_pending = sum(float(t["Pending"]) for t in targets)
        if total_pending <= 0:
            targets = []
        else:
            for t in targets:
                share = float(t["Pending"]) / total_pending
                alloc = round(min(remaining * share, float(t["Pending"])), 2)
                if alloc > 0:
                    cur.execute(
                        """INSERT INTO dbo.ReceiptInvoiceAllocations
                           (ReceiptId, InvoiceId, BusinessSegmentId, AllocatedAmount)
                           VALUES (?,?,?,?)""",
                        receipt_id, t["BillId"], t["BusinessSegmentId"], alloc)
            return

    for item in _pending_debits(cur, client_id):
        if remaining <= 0.01:
            break
        alloc = min(remaining, float(item["Pending"]))
        if item["BillType"] == "tax":
            cur.execute(
                """INSERT INTO dbo.ReceiptInvoiceAllocations
                   (ReceiptId, InvoiceId, BusinessSegmentId, AllocatedAmount)
                   VALUES (?,?,?,?)""",
                receipt_id, item["BillId"], item["BusinessSegmentId"], round(alloc, 2))
        else:
            cur.execute(
                """INSERT INTO dbo.ReceiptNonGstAllocations
                   (ReceiptId, NonGstBillId, BusinessSegmentId, AllocatedAmount)
                   VALUES (?,?,?,?)""",
                receipt_id, item["BillId"], item["BusinessSegmentId"], round(alloc, 2))
        remaining -= alloc


def list_receipts(client_id: int | None = None, limit: int = 5000) -> list[dict]:
    s = _snap()
    if s:
        return s.list_receipts(client_id, limit)
    with db.get_connection() as conn:
        cur = conn.cursor()
        if client_id:
            cur.execute(
                """SELECT TOP (?) r.*, c.ClientName FROM dbo.Receipts r
                   INNER JOIN dbo.ClientMaster c ON c.ClientId = r.ClientId
                   WHERE r.ClientId = ? ORDER BY r.ReceiptDate DESC""",
                limit, client_id)
        else:
            cur.execute(
                """SELECT TOP (?) r.*, c.ClientName FROM dbo.Receipts r
                   INNER JOIN dbo.ClientMaster c ON c.ClientId = r.ClientId
                   ORDER BY r.ReceiptDate DESC""",
                limit)
        return _rows(cur)


def get_receipt(receipt_id: int) -> dict | None:
    s = _snap()
    if s:
        return s.get_receipt(receipt_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT r.*, c.ClientName FROM dbo.Receipts r
               INNER JOIN dbo.ClientMaster c ON c.ClientId = r.ClientId
               WHERE r.ReceiptId = ?""",
            receipt_id)
        return _row(cur)


def get_receipt_allocations(receipt_id: int) -> dict:
    """Return invoice_ids allocated to this receipt."""
    s = _snap()
    if s:
        return s.get_receipt_allocations(receipt_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT InvoiceId FROM dbo.ReceiptInvoiceAllocations
               WHERE ReceiptId = ?""", receipt_id)
        inv_ids = [int(r[0]) for r in cur.fetchall()]
        return {"invoice_ids": inv_ids}


def delete_receipt(receipt_id: int) -> tuple[bool, str]:
    rcpt = get_receipt(receipt_id)
    if not rcpt:
        return False, "Receipt not found."
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM dbo.Receipts WHERE ReceiptId = ?", receipt_id)
        conn.commit()
    return True, f"Receipt {rcpt['ReceiptNumber']} deleted."


def client_open_invoices(client_id: int,
                         exclude_receipt_id: int | None = None) -> list[dict]:
    """Open tax invoices for receipt linking UI (all ages)."""
    s = _snap()
    if s:
        return s.client_open_invoices(client_id, exclude_receipt_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        return _invoice_pending(cur, client_id, exclude_receipt_id)


# --- Reports -----------------------------------------------------------------
def outstanding_dashboard() -> dict:
    s = _snap()
    if s:
        return s.outstanding_dashboard()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("EXEC dbo.sp_GetOutstandingDashboard")
        return _row(cur) or {}


def client_outstanding_list() -> list[dict]:
    s = _snap()
    if s:
        return s.client_outstanding_list()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM dbo.vw_ClientOutstanding ORDER BY Outstanding DESC, ClientName")
        return _rows(cur)


def client_summary(client_id: int) -> dict | None:
    s = _snap()
    if s:
        return s.client_summary(client_id)
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("EXEC dbo.sp_GetClientSummary ?", client_id)
        return _row(cur)


def client_ledger(client_id: int, from_date: str | None = None,
                  to_date: str | None = None,
                  segment_id: int | None = None) -> list[dict]:
    s = _snap()
    if s:
        return s.client_ledger(client_id, from_date, to_date, segment_id)
    fd = _parse_date(from_date) if from_date else None
    td = _parse_date(to_date) if to_date else None
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("EXEC dbo.sp_GetClientLedger ?, ?, ?, ?",
                    client_id, fd, td, segment_id)
        return _rows(cur)


def segment_outstanding_list(segment_id: int | None = None) -> list[dict]:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dbo.vw_SegmentOutstanding ORDER BY BusinessSegmentId")
        rows = _rows(cur)
    if segment_id:
        rows = [r for r in rows if r["BusinessSegmentId"] == segment_id]
    return rows


def client_invoices_for_receipt(client_id: int) -> list[dict]:
    """Open invoices for payment allocation UI."""
    with db.get_connection() as conn:
        cur = conn.cursor()
        return _invoice_pending(cur, client_id)


def ageing_analysis() -> list[dict]:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("EXEC dbo.sp_GetAgeingAnalysis")
        return _rows(cur)


def ageing_detail() -> list[dict]:
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM dbo.vw_InvoiceAgeing ORDER BY ClientName, InvoiceDate")
        return _rows(cur)


def sync_clients_from_workbook(clients: list) -> int:
    """Import clients from Excel workbook into ClientMaster."""
    count = 0
    for c in clients:
        upsert_client(c.name, c.gstin, c.address, mh=c.mh)
        count += 1
    return count

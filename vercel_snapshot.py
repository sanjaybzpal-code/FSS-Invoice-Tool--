"""Read-only JSON snapshot fallback for Vercel when Azure SQL is not configured."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from functools import lru_cache
from typing import Any

import runtime_paths as rp

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(HERE, "vercel_data", "snapshot.json")
OVERLAY_FILE = os.path.join(rp.data_root(), "vercel_overlay.json")

_CACHE: dict[str, list[dict]] | None = None


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
                "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:26], fmt).date()
        except ValueError:
            continue
    if "T" in s:
        try:
            return datetime.fromisoformat(s.replace("Z", "")).date()
        except ValueError:
            pass
    return None


def _coerce_row(row: dict) -> dict:
    out = dict(row)
    for k, v in list(out.items()):
        if v is None:
            continue
        if k.endswith("Date") or k.endswith("At"):
            parsed = _parse_date(v)
            if parsed:
                out[k] = parsed
        elif "Amount" in k:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = 0.0
    return out


def _load_overlay() -> dict[str, list[dict]]:
    if not os.path.isfile(OVERLAY_FILE):
        return {}
    try:
        with open(OVERLAY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("tables", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _merge_tables(base: list[dict], extra: list[dict], pk: str) -> list[dict]:
    if not extra:
        return list(base)
    seen = {r.get(pk) for r in base}
    out = list(base)
    for row in extra:
        rid = row.get(pk)
        if rid in seen:
            out = [r if r.get(pk) != rid else row for r in out]
        else:
            out.append(row)
            seen.add(rid)
    return out


def _invalidate() -> None:
    global _CACHE
    _CACHE = None
    _tables.cache_clear()


def _persist_overlay_rows(table: str, pk: str, rows: list[dict]) -> None:
    overlay = _load_overlay()
    overlay[table] = _merge_tables(overlay.get(table, []), rows, pk)
    os.makedirs(os.path.dirname(OVERLAY_FILE) or ".", exist_ok=True)
    with open(OVERLAY_FILE, "w", encoding="utf-8") as fh:
        json.dump({"tables": overlay}, fh, indent=2, default=str, ensure_ascii=False)
    _invalidate()


def _next_pk(table: str, pk: str) -> int:
    ids = [int(r.get(pk) or 0) for r in _tables().get(table, [])]
    return max(ids, default=0) + 1


@lru_cache(maxsize=1)
def _tables() -> dict[str, list[dict]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not os.path.isfile(SNAPSHOT_FILE):
        _CACHE = {}
        return _CACHE
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    base = raw.get("tables", {})
    overlay = _load_overlay()
    merged = {
        "BusinessSegments": _merge_tables(
            base.get("BusinessSegments", []), overlay.get("BusinessSegments", []), "BusinessSegmentId"),
        "ClientMaster": _merge_tables(
            base.get("ClientMaster", []), overlay.get("ClientMaster", []), "ClientId"),
        "TaxInvoices": _merge_tables(
            base.get("TaxInvoices", []), overlay.get("TaxInvoices", []), "InvoiceId"),
        "InvoiceLineItems": _merge_tables(
            base.get("InvoiceLineItems", []), overlay.get("InvoiceLineItems", []), "LineId"),
        "Receipts": _merge_tables(
            base.get("Receipts", []), overlay.get("Receipts", []), "ReceiptId"),
        "ReceiptInvoiceAllocations": _merge_tables(
            base.get("ReceiptInvoiceAllocations", []), overlay.get("ReceiptInvoiceAllocations", []), "AllocationId"),
        "NonGstBills": _merge_tables(
            base.get("NonGstBills", []), overlay.get("NonGstBills", []), "NonGstBillId"),
        "ReceiptNonGstAllocations": _merge_tables(
            base.get("ReceiptNonGstAllocations", []), overlay.get("ReceiptNonGstAllocations", []), "AllocationId"),
    }
    _CACHE = {name: [_coerce_row(r) for r in rows if isinstance(r, dict)]
              for name, rows in merged.items()}
    return _CACHE


def snapshot_available() -> bool:
    return os.path.isfile(SNAPSHOT_FILE)


def stats() -> dict[str, int]:
    t = _tables()
    return {
        "invoices": len(t.get("TaxInvoices", [])),
        "receipts": len(t.get("Receipts", [])),
        "clients": len(t.get("ClientMaster", [])),
    }


def _clients_map() -> dict[int, dict]:
    return {int(c["ClientId"]): c for c in _tables().get("ClientMaster", []) if c.get("ClientId") is not None}


def _segments_map() -> dict[int, dict]:
    return {int(s["BusinessSegmentId"]): s for s in _tables().get("BusinessSegments", [])}


def _enrich_invoice(inv: dict) -> dict:
    row = dict(inv)
    cid = inv.get("ClientId")
    sid = inv.get("BusinessSegmentId")
    clients = _clients_map()
    segs = _segments_map()
    row["ClientName"] = clients.get(int(cid), {}).get("ClientName", "") if cid else ""
    row["BusinessSegmentName"] = segs.get(int(sid), {}).get("BusinessSegmentName", "") if sid else ""
    return row


def _enrich_receipt(r: dict) -> dict:
    row = dict(r)
    cid = r.get("ClientId")
    row["ClientName"] = _clients_map().get(int(cid), {}).get("ClientName", "") if cid else ""
    return row


def _tax_invoices_only(invoices: list[dict]) -> list[dict]:
    return [i for i in invoices if (i.get("InvoiceType") or "TAX") != "PROFORMA"]


def _allocated(inv_id: int) -> float:
    total = 0.0
    for a in _tables().get("ReceiptInvoiceAllocations", []):
        if int(a.get("InvoiceId", 0)) == inv_id:
            total += float(a.get("AllocatedAmount") or 0)
    return total


# --- Public API (matches ledger_service) ---------------------------------------

def list_segments(active_only: bool = True) -> list[dict]:
    rows = list(_tables().get("BusinessSegments", []))
    if active_only:
        rows = [r for r in rows if r.get("IsActive", True)]
    return sorted(rows, key=lambda r: (r.get("SortOrder") or 0, r.get("BusinessSegmentName") or ""))


def list_clients(active_only: bool = True) -> list[dict]:
    rows = list(_tables().get("ClientMaster", []))
    if active_only:
        rows = [r for r in rows if r.get("IsActive", True)]
    return sorted(rows, key=lambda r: r.get("ClientName") or "")


def get_client(client_id: int) -> dict | None:
    for c in _tables().get("ClientMaster", []):
        if int(c.get("ClientId", 0)) == int(client_id):
            return dict(c)
    return None


def list_invoices(client_id: int | None = None, limit: int = 500,
                  segment_id: int | None = None,
                  invoice_type: str | None = None) -> list[dict]:
    rows = [_enrich_invoice(i) for i in _tables().get("TaxInvoices", [])]
    if client_id:
        rows = [r for r in rows if int(r.get("ClientId", 0)) == client_id]
    if segment_id:
        rows = [r for r in rows if int(r.get("BusinessSegmentId") or 0) == segment_id]
    if invoice_type:
        rows = [r for r in rows if (r.get("InvoiceType") or "TAX") == invoice_type.upper()]
    rows.sort(key=lambda r: (_parse_date(r.get("InvoiceDate")) or date.min, int(r.get("InvoiceId", 0))), reverse=True)
    return rows[:limit]


def get_invoice(invoice_id: int) -> dict | None:
    for inv in _tables().get("TaxInvoices", []):
        if int(inv.get("InvoiceId", 0)) == invoice_id:
            return _enrich_invoice(inv)
    return None


def get_invoice_line_items(invoice_id: int) -> list[dict]:
    items = [i for i in _tables().get("InvoiceLineItems", [])
             if int(i.get("InvoiceId", 0)) == invoice_id]
    return sorted(items, key=lambda r: int(r.get("SrNo") or 0))


def list_receipts(client_id: int | None = None, limit: int = 200) -> list[dict]:
    rows = [_enrich_receipt(r) for r in _tables().get("Receipts", [])]
    if client_id:
        rows = [r for r in rows if int(r.get("ClientId", 0)) == client_id]
    rows.sort(key=lambda r: (_parse_date(r.get("ReceiptDate")) or date.min,), reverse=True)
    return rows[:limit]


def get_receipt(receipt_id: int) -> dict | None:
    for r in _tables().get("Receipts", []):
        if int(r.get("ReceiptId", 0)) == receipt_id:
            return _enrich_receipt(r)
    return None


def list_non_gst_bills(client_id: int | None = None, limit: int = 500,
                       segment_id: int | None = None) -> list[dict]:
    rows = list(_tables().get("NonGstBills", []))
    clients = _clients_map()
    out = []
    for b in rows:
        row = dict(b)
        cid = b.get("ClientId")
        row["ClientName"] = clients.get(int(cid), {}).get("ClientName", "") if cid else ""
        out.append(row)
    if client_id:
        out = [r for r in out if int(r.get("ClientId", 0)) == client_id]
    if segment_id:
        out = [r for r in out if int(r.get("BusinessSegmentId") or 0) == segment_id]
    return out[:limit]


def peek_proforma_number() -> str:
    nums = []
    for inv in _tables().get("TaxInvoices", []):
        if (inv.get("InvoiceType") or "") == "PROFORMA":
            num = str(inv.get("InvoiceNumber") or "")
            if num.startswith("PF-"):
                try:
                    nums.append(int(num.split("-", 1)[1]))
                except ValueError:
                    pass
    n = max(nums) + 1 if nums else 1
    return f"PF-{n:05d}"


def next_proforma_number() -> str:
    return peek_proforma_number()


def max_tax_invoice_number() -> int:
    n = 0
    for inv in _tables().get("TaxInvoices", []):
        if (inv.get("InvoiceType") or "TAX").upper() == "PROFORMA":
            continue
        try:
            n = max(n, int(str(inv.get("InvoiceNumber"))))
        except (TypeError, ValueError):
            continue
    return n


def upsert_client(name: str, gstin: str = "", address: str = "",
                  contact: str = "", email: str = "", mobile: str = "",
                  mh: bool = False) -> int:
    name = (name or "").strip()
    for c in _tables().get("ClientMaster", []):
        if (c.get("ClientName") or "").strip().lower() == name.lower():
            cid = int(c["ClientId"])
            updated = dict(c)
            if gstin:
                updated["GSTIN"] = gstin
            if address:
                updated["Address"] = address
            if contact:
                updated["ContactPerson"] = contact
            if email:
                updated["Email"] = email
            if mobile:
                updated["Mobile"] = mobile
            updated["MhState"] = 1 if mh else int(c.get("MhState") or 0)
            _persist_overlay_rows("ClientMaster", "ClientId", [updated])
            return cid
    cid = _next_pk("ClientMaster", "ClientId")
    row = {
        "ClientId": cid,
        "ClientName": name,
        "GSTIN": gstin or "",
        "Address": address or "",
        "ContactPerson": contact or "",
        "Email": email or "",
        "Mobile": mobile or "",
        "MhState": 1 if mh else 0,
        "IsActive": 1,
        "OpeningBalance": 0,
    }
    _persist_overlay_rows("ClientMaster", "ClientId", [row])
    return cid


def record_tax_invoice(client_id: int, invoice_number: str, invoice_date: str,
                       taxable: float, cgst: float, sgst: float, igst: float,
                       total: float, supply_type: str, line_items: list[dict],
                       pdf_path: str = "", excel_path: str = "",
                       created_by: str = "", segment_id: int = 1,
                       invoice_type: str = "TAX") -> int:
    from datetime import timedelta
    inv_date = _parse_date(invoice_date) or date.today()
    invoice_id = _next_pk("TaxInvoices", "InvoiceId")
    inv = {
        "InvoiceId": invoice_id,
        "ClientId": int(client_id),
        "InvoiceNumber": str(invoice_number),
        "InvoiceDate": inv_date,
        "DueDate": inv_date + timedelta(days=30),
        "PaymentTermsDays": 30,
        "TaxableAmount": float(taxable),
        "CGSTAmount": float(cgst),
        "SGSTAmount": float(sgst),
        "IGSTAmount": float(igst),
        "TotalAmount": float(total),
        "SupplyType": supply_type,
        "PdfPath": pdf_path or "",
        "ExcelPath": excel_path or "",
        "CreatedBy": created_by or "",
        "BusinessSegmentId": segment_id or 1,
        "InvoiceType": (invoice_type or "TAX").upper(),
    }
    _persist_overlay_rows("TaxInvoices", "InvoiceId", [inv])
    line_id = _next_pk("InvoiceLineItems", "LineId")
    lines = []
    for i, it in enumerate(line_items, 1):
        lines.append({
            "LineId": line_id,
            "InvoiceId": invoice_id,
            "SrNo": i,
            "Particulars": it.get("particulars") or it.get("Particulars") or "",
            "WorkDate": _parse_date(it.get("date") or it.get("WorkDate")),
            "Amount": float(it.get("amount") or it.get("Amount") or 0),
        })
        line_id += 1
    if lines:
        _persist_overlay_rows("InvoiceLineItems", "LineId", lines)
    return invoice_id


def peek_receipt_number() -> str:
    nums = []
    for r in _tables().get("Receipts", []):
        num = str(r.get("ReceiptNumber") or "")
        if num.upper().startswith("RCP-"):
            try:
                nums.append(int(num.split("-", 1)[1]))
            except ValueError:
                pass
    n = max(nums) + 1 if nums else 1
    return f"RCP-{n:05d}"


def next_receipt_number() -> str:
    return peek_receipt_number()


def peek_non_gst_bill_number() -> str:
    nums = []
    for b in _tables().get("NonGstBills", []):
        num = str(b.get("BillNumber") or "")
        if num.upper().startswith("NGB-"):
            try:
                nums.append(int(num.split("-", 1)[1]))
            except ValueError:
                pass
    n = max(nums) + 1 if nums else 1
    return f"NGB-{n:05d}"


def get_receipt_allocations(receipt_id: int) -> dict:
    inv_ids = [
        int(a["InvoiceId"])
        for a in _tables().get("ReceiptInvoiceAllocations", [])
        if int(a.get("ReceiptId", 0)) == receipt_id and a.get("InvoiceId") is not None
    ]
    return {"invoice_ids": inv_ids}


def outstanding_dashboard() -> dict:
    invoices = _tax_invoices_only(_tables().get("TaxInvoices", []))
    receipts = _tables().get("Receipts", [])
    total_invoiced = sum(float(i.get("TotalAmount") or 0) for i in invoices)
    total_received = sum(float(r.get("AmountReceived") or 0) for r in receipts)
    non_gst = sum(float(b.get("Amount") or 0) for b in _tables().get("NonGstBills", []))
    return {
        "TotalInvoiced": total_invoiced,
        "TotalReceived": total_received,
        "TotalOutstanding": round(total_invoiced + non_gst - total_received, 2),
        "TotalNonGst": non_gst,
    }


def client_outstanding_list() -> list[dict]:
    clients = _clients_map()
    result: dict[int, dict] = {}
    for inv in _tax_invoices_only(_tables().get("TaxInvoices", [])):
        cid = int(inv.get("ClientId", 0))
        pending = float(inv.get("TotalAmount") or 0) - _allocated(int(inv.get("InvoiceId", 0)))
        if pending <= 0.01:
            continue
        if cid not in result:
            c = clients.get(cid, {})
            result[cid] = {
                "ClientId": cid,
                "ClientName": c.get("ClientName", ""),
                "GSTIN": c.get("GSTIN", ""),
                "TaxInvoiced": 0.0,
                "NonGstAmount": 0.0,
                "TotalReceived": 0.0,
                "Outstanding": 0.0,
            }
        result[cid]["TaxInvoiced"] += float(inv.get("TotalAmount") or 0)
        result[cid]["Outstanding"] += pending
    for r in _tables().get("Receipts", []):
        cid = int(r.get("ClientId", 0))
        if cid in result:
            result[cid]["TotalReceived"] += float(r.get("AmountReceived") or 0)
    rows = list(result.values())
    rows.sort(key=lambda r: (-float(r.get("Outstanding") or 0), r.get("ClientName") or ""))
    return rows


def client_summary(client_id: int) -> dict | None:
    c = _clients_map().get(client_id)
    if not c:
        return None
    invs = [i for i in _tax_invoices_only(_tables().get("TaxInvoices", []))
            if int(i.get("ClientId", 0)) == client_id]
    rcps = [r for r in _tables().get("Receipts", []) if int(r.get("ClientId", 0)) == client_id]
    invoiced = sum(float(i.get("TotalAmount") or 0) for i in invs)
    received = sum(float(r.get("AmountReceived") or 0) for r in rcps)
    pending = sum(float(i.get("TotalAmount") or 0) - _allocated(int(i.get("InvoiceId", 0))) for i in invs)
    return {
        "ClientId": client_id,
        "ClientName": c.get("ClientName"),
        "TotalTaxInvoiced": invoiced,
        "TotalReceived": received,
        "Outstanding": round(pending, 2),
    }


def client_ledger(client_id: int, from_date: str | None = None,
                  to_date: str | None = None,
                  segment_id: int | None = None) -> list[dict]:
    fd, td = _parse_date(from_date), _parse_date(to_date)
    entries: list[dict] = []
    for inv in _tables().get("TaxInvoices", []):
        if int(inv.get("ClientId", 0)) != client_id:
            continue
        if (inv.get("InvoiceType") or "TAX") == "PROFORMA":
            continue
        if segment_id and int(inv.get("BusinessSegmentId") or 0) != segment_id:
            continue
        d = _parse_date(inv.get("InvoiceDate"))
        if fd and d and d < fd:
            continue
        if td and d and d > td:
            continue
        entries.append({
            "TxnDate": inv.get("InvoiceDate"),
            "Particulars": f"Invoice {inv.get('InvoiceNumber')}",
            "Debit": float(inv.get("TotalAmount") or 0),
            "Credit": 0.0,
        })
    for r in _tables().get("Receipts", []):
        if int(r.get("ClientId", 0)) != client_id:
            continue
        d = _parse_date(r.get("ReceiptDate"))
        if fd and d and d < fd:
            continue
        if td and d and d > td:
            continue
        entries.append({
            "TxnDate": r.get("ReceiptDate"),
            "Particulars": f"Receipt {r.get('ReceiptNumber')}",
            "Debit": 0.0,
            "Credit": float(r.get("AmountReceived") or 0),
        })
    entries.sort(key=lambda e: str(e.get("TxnDate") or ""))
    bal = 0.0
    for e in entries:
        bal += float(e.get("Debit") or 0) - float(e.get("Credit") or 0)
        e["Balance"] = round(bal, 2)
    return entries


def executive_summary() -> dict:
    dash = outstanding_dashboard()
    return {
        "TotalInvoiced": dash.get("TotalInvoiced", 0),
        "TotalReceived": dash.get("TotalReceived", 0),
        "TotalOutstanding": dash.get("TotalOutstanding", 0),
        "TotalActiveClients": len(_clients_map()),
        "TotalGstReceivable": 0,
        "TotalTdsDeducted": 0,
    }


def executive_segment_cards() -> dict[str, Any]:
    segs = list_segments()
    cards = {"segments": [], "total_revenue": 0.0, "total_outstanding": 0.0}
    for s in segs:
        sid = int(s["BusinessSegmentId"])
        rev = sum(float(i.get("TotalAmount") or 0)
                  for i in _tax_invoices_only(_tables().get("TaxInvoices", []))
                  if int(i.get("BusinessSegmentId") or 0) == sid)
        out = sum(float(i.get("TotalAmount") or 0) - _allocated(int(i.get("InvoiceId", 0)))
                  for i in _tax_invoices_only(_tables().get("TaxInvoices", []))
                  if int(i.get("BusinessSegmentId") or 0) == sid)
        card = dict(s)
        card["TotalRevenue"] = rev
        card["Outstanding"] = round(out, 2)
        cards["segments"].append(card)
        cards["total_revenue"] += rev
        cards["total_outstanding"] += out
    return cards


def reminder_dashboard() -> dict:
    return {"DueToday": 0, "DueThisWeek": 0, "Overdue": 0, "CriticalOverdue": 0}

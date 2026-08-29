"""Find invoice PDF/Excel on disk, or rebuild them from saved invoice data.

Office PC: files live in the Invoices folder.
Vercel: those files are not deployed, so download regenerates into a writable temp folder.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import date, datetime

import runtime_paths as rp

HERE = rp.HERE


def load_config() -> dict:
    if os.environ.get("CONFIG_JSON"):
        try:
            return json.loads(os.environ["CONFIG_JSON"])
        except json.JSONDecodeError:
            pass
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if rp.is_vercel():
        cfg.setdefault("paths", {})["output_folder"] = rp.invoices_dir()
    return cfg


def write_folder() -> str:
    return rp.invoices_dir()


def search_folders() -> list[str]:
    folders = []
    for path in (os.path.join(HERE, "Invoices"), write_folder()):
        if path not in folders:
            folders.append(path)
    return folders


def _safe_name(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9 _.-]", "", text or "").strip()
    return re.sub(r"\s+", "_", text)[:60] or "invoice"


def _fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")
    s = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return s


def _is_proforma(inv: dict) -> bool:
    return (inv.get("InvoiceType") or "TAX").upper() == "PROFORMA"


def _prefixes(inv: dict) -> list[str]:
    return ["Proforma", "Invoice"] if _is_proforma(inv) else ["Invoice", "Proforma"]


def _resolve_one(stored: str | None, folders: list[str]) -> str | None:
    if not stored:
        return None
    stored = str(stored).strip()
    if os.path.isfile(stored):
        return stored
    base = os.path.basename(stored.replace("\\", "/"))
    if not base:
        return None
    for folder in folders:
        cand = os.path.join(folder, base)
        if os.path.isfile(cand):
            return cand
    return None


def _glob_by_number(inv: dict, folders: list[str], ext: str) -> str | None:
    num = str(inv.get("InvoiceNumber") or "").strip()
    if not num:
        return None
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for prefix in _prefixes(inv):
            matches = glob.glob(os.path.join(folder, f"{prefix}_{num}_*.{ext}"))
            if matches:
                return sorted(matches)[-1]
    return None


def find_paths(inv: dict) -> dict[str, str | None]:
    folders = search_folders()
    pdf = _resolve_one(inv.get("PdfPath"), folders) or _glob_by_number(inv, folders, "pdf")
    excel = _resolve_one(inv.get("ExcelPath"), folders) or _glob_by_number(inv, folders, "xlsx")
    return {"pdf": pdf, "excel": excel}


def enrich_for_archive(rows: list[dict]) -> list[dict]:
    """Always offer PDF/Excel — files are rebuilt on download if missing."""
    out = []
    for inv in rows:
        inv = dict(inv)
        paths = find_paths(inv)
        inv["has_pdf"] = True
        inv["has_excel"] = True
        inv["pdf_name"] = os.path.basename(paths["pdf"]) if paths["pdf"] else ""
        inv["excel_name"] = os.path.basename(paths["excel"]) if paths["excel"] else ""
        out.append(inv)
    return out


def _line_items(invoice_id: int, inv: dict) -> list[dict]:
    import ledger_service as ls
    raw = ls.get_invoice_line_items(invoice_id)
    items = []
    for it in raw:
        particulars = (it.get("particulars") or it.get("Particulars") or "").strip()
        amount = it.get("amount") if it.get("amount") is not None else it.get("Amount")
        work = it.get("date") or it.get("WorkDate") or ""
        if not particulars and amount is None:
            continue
        items.append({
            "particulars": particulars or "As per invoice",
            "date": _fmt_date(work),
            "amount": float(amount or 0),
        })
    if items:
        return items
    amt = float(inv.get("TaxableAmount") or 0) or float(inv.get("TotalAmount") or 0)
    return [{"particulars": "As per invoice", "date": _fmt_date(inv.get("InvoiceDate")),
             "amount": amt}]


def _client_obj(inv: dict):
    import clients as clients_mod
    import ledger_service as ls
    row = None
    try:
        cid = inv.get("ClientId")
        if cid:
            row = ls.get_client(int(cid))
    except Exception:
        row = None
    name = (row or {}).get("ClientName") or inv.get("ClientName") or "Client"
    gstin = (row or {}).get("GSTIN") or inv.get("GSTIN") or ""
    address = (row or {}).get("Address") or inv.get("Address") or ""
    supply = str(inv.get("SupplyType") or "").upper()
    if "IGST" in supply:
        mh = False
    elif "CGST" in supply or "SGST" in supply:
        mh = True
    else:
        mh = bool((row or {}).get("MhState"))
    return clients_mod.Client(
        code=str(inv.get("ClientId") or ""),
        name=name,
        gstin=gstin,
        mh=mh,
        address=address,
    )


def _target_paths(inv: dict) -> tuple[str, str]:
    folder = write_folder()
    os.makedirs(folder, exist_ok=True)
    prefix = "Proforma" if _is_proforma(inv) else "Invoice"
    client = _client_obj(inv)
    base = f"{prefix}_{_safe_name(str(inv.get('InvoiceNumber')))}_{_safe_name(client.name)}"
    return os.path.join(folder, base + ".pdf"), os.path.join(folder, base + ".xlsx")


def rebuild(inv: dict, fmt: str | None = None) -> dict[str, str]:
    from generator import generate_invoice
    from pdf_generator import generate_pdf_invoice

    config = load_config()
    client = _client_obj(inv)
    inv_no = str(inv.get("InvoiceNumber") or "")
    inv_date = _fmt_date(inv.get("InvoiceDate"))
    items = _line_items(int(inv["InvoiceId"]), inv)
    pdf_path, xlsx_path = _target_paths(inv)
    doc_kw = {"document_type": "proforma" if _is_proforma(inv) else "tax"}
    if fmt in (None, "pdf"):
        generate_pdf_invoice(config, client, inv_no, inv_date, items, pdf_path, **doc_kw)
    if fmt in (None, "excel"):
        generate_invoice(config, client, inv_no, inv_date, items, xlsx_path, **doc_kw)
    return {"pdf": pdf_path, "excel": xlsx_path}


def ensure_download(inv: dict, fmt: str) -> str:
    paths = find_paths(inv)
    existing = paths["pdf"] if fmt == "pdf" else paths["excel"]
    if existing:
        return existing
    built = rebuild(inv, fmt=fmt)
    path = built["pdf"] if fmt == "pdf" else built["excel"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"Could not build {fmt.upper()} for invoice {inv.get('InvoiceNumber')}")
    return path

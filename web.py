"""FSS Invoice & Accounts Suite — web application.

Tax invoice generation + full accounts receivable (ledger, TDS, GST,
reminders, WhatsApp, profitability, executive dashboard).
Run via launch.pyw or: python web.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import webbrowser
from dataclasses import asdict
from datetime import datetime
from threading import Timer

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import auth
import clients as clients_mod
from accounts import accounts_bp
from ar_routes import ar_bp
from segment_routes import segment_bp
from generator import generate_invoice
from pdf_generator import generate_pdf_invoice
from invoice_core import compute_totals, next_invoice_number, record_invoice

import runtime_paths as rp

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

app = Flask(__name__)
app.secret_key = auth.get_secret_key()
app.register_blueprint(accounts_bp)
app.register_blueprint(ar_bp)
app.register_blueprint(segment_bp)


@app.template_filter("fdate")
def format_date(value, fmt="%d-%m-%Y"):
    """Safe date display — datetime or ISO string."""
    if not value:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime(fmt)
        except Exception:
            pass
    s = str(value).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    parts = s.replace("/", "-").split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        y, m, d = parts
        return f"{d}-{m}-{y}"
    return s

try:
    import db as _db
    _db.migrate()
except Exception:  # noqa: BLE001 - app still works if DB temporarily unavailable
    pass


@app.context_processor
def inject_globals():
    """Shared template variables (user, role, admin, segment) on every page."""
    u = auth.current_user()
    return {
        "user": u,
        "is_admin": auth.is_admin(u) if u else False,
        "user_role": auth.get_role(u) if u else "",
        "user_segment_id": auth.user_segment_id(u) if u else None,
        "can_expenses": auth.can_expenses(u) if u else False,
        "can_profit": auth.can_profit(u) if u else False,
        "can_management": auth.can_management_dashboard(u) if u else False,
        "can_edit_invoices": bool(
            u and auth.normalize_role(auth.get_role(u)) != auth.ROLE_VIEWER
        ),
    }


def load_config() -> dict:
    if os.environ.get("CONFIG_JSON"):
        try:
            return json.loads(os.environ["CONFIG_JSON"])
        except json.JSONDecodeError:
            pass
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if rp.is_vercel() or os.environ.get("FSS_DATA_DIR"):
        cfg.setdefault("paths", {})["output_folder"] = rp.invoices_dir()
    return cfg


def save_config(config: dict) -> None:
    if rp.is_vercel() or os.environ.get("FSS_DATA_DIR"):
        return
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4, ensure_ascii=False)
    except OSError:
        return


def safe_filename(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9 _.-]", "", text or "").strip()
    return re.sub(r"\s+", "_", text)[:60] or "invoice"


def _workbook_path(config: dict) -> str:
    rel = config["paths"]["clients_workbook"]
    return rel if os.path.isabs(rel) else os.path.join(HERE, rel)


def _output_folder(config: dict) -> str:
    rel = config["paths"]["output_folder"]
    return rel if os.path.isabs(rel) else os.path.join(HERE, rel)


def get_clients(config: dict):
    return clients_mod.load_clients(
        _workbook_path(config), config["paths"]["clients_sheet"])


def _server_port(config: dict) -> int:
    return int(config.get("server", {}).get("port", 5000))


def lan_ip() -> str:
    """Best-effort LAN IP of this machine (for the shareable team URL)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def share_url(config: dict) -> str:
    public = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public + "/"
    return f"http://{lan_ip()}:{_server_port(config)}/"


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not auth.needs_setup():
        return redirect(url_for("login"))
    if request.method == "POST":
        ok, msg = auth.create_user(
            request.form.get("username") or "",
            request.form.get("password") or "",
            request.form.get("confirm") or "",
            role=auth.ROLE_ADMIN)
        if ok:
            session["user"] = (request.form.get("username") or "").strip()
            return redirect(url_for("index"))
        flash(msg)
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.needs_setup():
        return redirect(url_for("setup"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if auth.verify(username, password):
            session["user"] = username
            return redirect(url_for("index"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@auth.login_required
def change_password():
    if request.method == "POST":
        ok, msg = auth.change_password(
            auth.current_user(),
            request.form.get("old_password") or "",
            request.form.get("new_password") or "")
        flash(msg)
        if ok:
            return redirect(url_for("index"))
    return render_template("change_password.html", user=auth.current_user())


@app.route("/")
@auth.login_required
def index():
    config = load_config()
    try:
        client_list, msg = get_clients(config)
    except Exception as exc:  # noqa: BLE001
        client_list, msg = [], f"Could not load clients: {exc}"
    clients_data = [asdict(c) for c in client_list]
    segments = []
    try:
        import segment_service as seg_svc
        all_segs = seg_svc.list_segments()
        seg_filter = auth.user_segment_id(auth.current_user())
        segments = [s for s in all_segs if not seg_filter or s["BusinessSegmentId"] == seg_filter]
    except Exception:  # noqa: BLE001
        segments = [
            {"BusinessSegmentId": 1, "BusinessSegmentName": "FSS Calculation"},
            {"BusinessSegmentId": 2, "BusinessSegmentName": "FSS Consultancy"},
            {"BusinessSegmentId": 3, "BusinessSegmentName": "Next Gen"},
        ]
    db_ok, db_msg = False, ""
    try:
        import db as _db
        db_ok, db_msg = _db.test_connection()
    except Exception as exc:  # noqa: BLE001
        db_msg = str(exc)
    next_pf = "PF-00001"
    try:
        import ledger_service as ls
        next_pf = ls.peek_proforma_number()
    except Exception:  # noqa: BLE001
        pass
    doc_type = (request.args.get("type") or "tax").strip().lower()
    if doc_type not in ("tax", "proforma"):
        doc_type = "tax"
    return render_template(
        "index.html",
        clients=clients_data,
        clients_json=json.dumps(clients_data),
        segments=segments,
        status=msg,
        seller=config["seller"],
        tax=config["tax"],
        next_number=next_invoice_number(config, _output_folder(config)),
        next_proforma=next_pf,
        default_doc_type=doc_type,
        today=datetime.today().strftime("%d-%m-%Y"),
        user=auth.current_user(),
        is_admin=auth.is_admin(auth.current_user()),
        share_url=share_url(config),
        db_ok=db_ok,
        db_message=db_msg,
    )


@app.route("/users")
@auth.admin_required
def users():
    return render_template("users.html", users=auth.list_users(),
                           me=auth.current_user())


@app.route("/users/add", methods=["POST"])
@auth.admin_required
def users_add():
    role = request.form.get("role") or auth.ROLE_ACCOUNTS
    ok, msg = auth.create_user(
        request.form.get("username") or "",
        request.form.get("password") or "",
        request.form.get("confirm") or "",
        role=role)
    flash(msg)
    return redirect(url_for("users"))


@app.route("/users/delete", methods=["POST"])
@auth.admin_required
def users_delete():
    ok, msg = auth.delete_user(request.form.get("username") or "",
                               auth.current_user())
    flash(msg)
    return redirect(url_for("users"))


@app.route("/api/reload")
@auth.login_required
def api_reload():
    config = load_config()
    try:
        client_list, msg = get_clients(config)
        return jsonify(ok=True, message=msg,
                       clients=[asdict(c) for c in client_list])
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, message=str(exc)), 500


def _resolve_client(config, data):
    """Return a Client object from either an existing selection or new-client fields."""
    if (data.get("client_mode") or "existing") == "new":
        nc = data.get("new_client") or {}
        name = (nc.get("name") or "").strip()
        if not name:
            raise ValueError("Enter the new client's name.")
        gstin = (nc.get("gstin") or "").strip()
        address = (nc.get("address") or "").strip()
        mh = bool(nc.get("mh"))
        if data.get("save_client"):
            return clients_mod.save_custom_client(name, gstin, address, mh)
        return clients_mod.Client(code="C", name=name, gstin=gstin,
                                  mh=mh, address=address)

    name = (data.get("client") or "").strip()
    if not name:
        raise ValueError("Please select a client.")
    client_list, _ = get_clients(config)
    client = next((c for c in client_list if c.name == name), None)
    if client is None:
        raise ValueError("Unknown client selected.")
    return client


def _collect_items(raw_items):
    items = []
    for it in raw_items:
        particulars = (it.get("particulars") or "").strip()
        amount_raw = str(it.get("amount") or "").strip()
        if not particulars and not amount_raw:
            continue
        if not particulars:
            raise ValueError("A row has an amount but no particulars.")
        try:
            amount = float(amount_raw)
        except ValueError:
            raise ValueError(f"Invalid amount for: {particulars}")
        if amount < 0:
            raise ValueError(f"Negative amount for: {particulars}")
        if amount == 0:
            continue
        items.append({"particulars": particulars,
                      "date": (it.get("date") or "").strip(),
                      "amount": amount})
    return items


@app.errorhandler(Exception)
def _json_api_errors(exc):
    from werkzeug.exceptions import HTTPException
    if not (request.path or "").startswith("/api/"):
        if isinstance(exc, HTTPException):
            return exc
        raise exc
    code = exc.code if isinstance(exc, HTTPException) else 500
    msg = exc.description if isinstance(exc, HTTPException) else str(exc)
    return jsonify(ok=False, message=msg or "Server error"), code


@app.route("/api/generate", methods=["POST"])
@auth.login_required
def api_generate():
    config = load_config()
    data = request.get_json(silent=True) or {}

    invoice_no = (data.get("invoice_no") or "").strip()
    invoice_date = (data.get("invoice_date") or "").strip()
    formats = data.get("formats") or ["excel"]

    try:
        client = _resolve_client(config, data)
        items = _collect_items(data.get("items") or [])
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, message=f"Client error: {exc}"), 500

    if not items:
        return jsonify(ok=False,
                       message="Add at least one particular with an amount."), 400

    segment_id = int(data.get("segment_id") or 0)
    if not segment_id:
        return jsonify(ok=False, message="Business Segment is required."), 400
    user_seg = auth.user_segment_id(auth.current_user())
    if user_seg and segment_id != user_seg:
        return jsonify(ok=False, message="You can only create invoices for your segment."), 403

    document_type = (data.get("document_type") or "tax").strip().lower()
    is_proforma = document_type == "proforma"

    out_folder = _output_folder(config)
    os.makedirs(out_folder, exist_ok=True)
    import ledger_service as ls
    existing = None
    edit_id = int(data.get("invoice_id") or 0)
    if edit_id:
        existing = ls.get_invoice(edit_id)
        if not existing:
            return jsonify(ok=False, message="Saved invoice not found."), 404
        if user_seg and existing.get("BusinessSegmentId") and int(existing["BusinessSegmentId"]) != user_seg:
            return jsonify(ok=False, message="You cannot edit invoices from other segments."), 403
        if not invoice_no:
            invoice_no = str(existing.get("InvoiceNumber") or "")
    if not invoice_no:
        if is_proforma:
            invoice_no = ls.next_proforma_number()
        else:
            invoice_no = str(max(
                next_invoice_number(config, out_folder),
                ls.max_tax_invoice_number() + 1,
            ))
    if existing is None:
        existing = ls.find_invoice_by_number(invoice_no)
        if existing and user_seg and existing.get("BusinessSegmentId") and int(existing["BusinessSegmentId"]) != user_seg:
            return jsonify(ok=False, message="You cannot edit invoices from other segments."), 403
    is_update = existing is not None
    prefix = "Proforma" if is_proforma else "Invoice"
    base = f"{prefix}_{safe_filename(invoice_no)}_{safe_filename(client.name)}"

    totals = compute_totals(config, client, items)
    files = []
    doc_kw = {"document_type": "proforma" if is_proforma else "tax"}
    try:
        if "excel" in formats:
            xlsx = os.path.join(out_folder, base + ".xlsx")
            generate_invoice(config, client, invoice_no, invoice_date, items, xlsx, **doc_kw)
            files.append(base + ".xlsx")
        if "pdf" in formats:
            pdf = os.path.join(out_folder, base + ".pdf")
            generate_pdf_invoice(config, client, invoice_no, invoice_date, items, pdf, **doc_kw)
            files.append(base + ".pdf")
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, message=f"Generation failed: {exc}"), 500

    if not files:
        return jsonify(ok=False, message="Select at least one format."), 400

    if not is_proforma:
        record_invoice(invoice_no, client.name, totals.total, invoice_date, files)

    # Post to SQL — update existing number, or insert a new invoice
    try:
        client_id = ls.upsert_client(
            client.name, client.gstin, client.address, mh=client.mh)
        pdf_p = os.path.join(out_folder, base + ".pdf") if any(f.endswith(".pdf") for f in files) else ""
        xlsx_p = os.path.join(out_folder, base + ".xlsx") if any(f.endswith(".xlsx") for f in files) else ""
        inv_type = "PROFORMA" if is_proforma else "TAX"
        if is_update and existing:
            ls.update_invoice(
                int(existing["InvoiceId"]),
                segment_id=segment_id,
                invoice_date=invoice_date,
                client_id=client_id,
                taxable_amount=totals.subtotal,
                cgst_amount=totals.cgst,
                sgst_amount=totals.sgst,
                igst_amount=totals.igst,
                total_amount=totals.total,
                supply_type=totals.supply_type,
                line_items=items,
                invoice_number=invoice_no,
                invoice_type=inv_type,
                pdf_path=pdf_p,
                excel_path=xlsx_p,
            )
        else:
            ls.record_tax_invoice(
                client_id, invoice_no, invoice_date,
                totals.subtotal, totals.cgst, totals.sgst, totals.igst,
                totals.total, totals.supply_type, items,
                pdf_p, xlsx_p, auth.current_user(), segment_id=segment_id,
                invoice_type=inv_type)
        import audit as audit_mod
        audit_mod.log(auth.current_user(),
                      "edit_invoice" if is_update else (
                          "generate_proforma" if is_proforma else "generate_invoice"),
                      "invoice", str(invoice_no), f"total={totals.total}")
    except Exception as exc:  # noqa: BLE001
        return jsonify(
            ok=False,
            message=(
                f"PDF/Excel ban gaya, lekin invoice database mein save nahi hua: {exc}. "
                "Office PC par SQL Server chalu rakho, ya Vercel par DATABASE_URL (Postgres) set karo."
            ),
            files=files,
        ), 500

    if not is_update:
        if not is_proforma:
            try:
                if int(invoice_no) >= config["invoice"]["next_number"]:
                    config["invoice"]["next_number"] = int(invoice_no) + 1
                    save_config(config)
            except ValueError:
                pass

    next_num = ls.peek_proforma_number() if is_proforma else max(
        next_invoice_number(config, out_folder),
        ls.max_tax_invoice_number() + 1,
    )
    return jsonify(ok=True, files=files, total=totals.total,
                   subtotal=totals.subtotal, cgst=totals.cgst,
                   sgst=totals.sgst, igst=totals.igst,
                   supply_type=totals.supply_type,
                   document_type=document_type,
                   updated=is_update,
                   next_number=next_num)


def _invoice_json(inv: dict, items: list[dict] | None = None) -> dict:
    import ledger_service as ls
    if items is None:
        items = ls.get_invoice_line_items(int(inv["InvoiceId"]))
        if not items:
            amt = float(inv.get("TaxableAmount") or 0) or float(inv.get("TotalAmount") or 0)
            items = [{"Particulars": "As per invoice",
                      "WorkDate": inv.get("InvoiceDate"), "Amount": amt}]
    lines = []
    for it in items:
        lines.append({
            "particulars": it.get("Particulars") or it.get("particulars") or "",
            "date": format_date(it.get("WorkDate") or it.get("date") or ""),
            "amount": float(it.get("Amount") if it.get("Amount") is not None else it.get("amount") or 0),
        })
    return {
        "invoice_id": int(inv["InvoiceId"]),
        "invoice_no": str(inv.get("InvoiceNumber") or ""),
        "invoice_date": format_date(inv.get("InvoiceDate")),
        "client_id": inv.get("ClientId"),
        "client": inv.get("ClientName") or "",
        "segment_id": inv.get("BusinessSegmentId"),
        "document_type": "proforma" if (inv.get("InvoiceType") or "TAX").upper() == "PROFORMA" else "tax",
        "supply_type": inv.get("SupplyType") or "",
        "items": lines,
    }


@app.route("/api/invoices/recent")
@auth.login_required
def api_invoices_recent():
    import ledger_service as ls
    try:
        seg_id = auth.user_segment_id(auth.current_user())
        rows = ls.list_invoices(limit=500, segment_id=seg_id)
        out = []
        for inv in rows:
            out.append({
                "invoice_id": int(inv["InvoiceId"]),
                "invoice_no": str(inv.get("InvoiceNumber") or ""),
                "invoice_date": format_date(inv.get("InvoiceDate")),
                "client": inv.get("ClientName") or "",
                "total": float(inv.get("TotalAmount") or 0),
                "document_type": "proforma" if (inv.get("InvoiceType") or "TAX").upper() == "PROFORMA" else "tax",
            })
        return jsonify(ok=True, invoices=out)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, message=str(exc), invoices=[]), 500


@app.route("/api/invoice/<int:invoice_id>")
@auth.login_required
def api_invoice_get(invoice_id):
    import ledger_service as ls
    try:
        inv = ls.get_invoice(invoice_id)
        if not inv:
            return jsonify(ok=False, message="Invoice not found."), 404
        seg_id = auth.user_segment_id(auth.current_user())
        if seg_id and inv.get("BusinessSegmentId") and int(inv["BusinessSegmentId"]) != seg_id:
            return jsonify(ok=False, message="You cannot access invoices from other segments."), 403
        return jsonify(ok=True, invoice=_invoice_json(inv))
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, message=str(exc)), 500


@app.route("/health")
def health():
    """Simple uptime check for live deployment monitoring."""
    db_ok, db_msg = False, "not configured"
    try:
        import db as _db
        if rp.is_vercel() and not _db.vercel_db_configured():
            if _db.use_snapshot_fallback():
                db_ok, db_msg = _db.test_connection()
            else:
                db_msg = (
                    "Set AZURE_SQL_CONNECTION_STRING on Vercel, "
                    "or run export_vercel_snapshot.py and redeploy.")
        else:
            db_ok, db_msg = _db.test_connection()
    except Exception as exc:  # noqa: BLE001
        db_msg = str(exc)
    return jsonify(ok=True, service="FSS Invoice Tool", database=db_ok, db_message=db_msg)


@app.route("/download/<path:filename>")
@auth.login_required
def download(filename):
    config = load_config()
    return send_file(os.path.join(_output_folder(config), filename),
                     as_attachment=True)


def _open_browser(port):
    webbrowser.open_new(f"http://127.0.0.1:{port}/")


if __name__ == "__main__":
    cfg = load_config()
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = _server_port(cfg)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        Timer(1.0, lambda: _open_browser(port)).start()
    app.run(host=host, port=port, debug=False)

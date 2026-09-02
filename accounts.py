"""Accounts module — Client Ledger, Receipts, Outstanding, Ageing."""

from __future__ import annotations

import os
from datetime import date

from flask import (Blueprint, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

import auth
import invoice_files as inf
import ledger_service as ls
from ledger_reports import export_ledger_excel, export_ledger_pdf

accounts_bp = Blueprint("accounts", __name__, url_prefix="/accounts")

HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(HERE, "Exports")


def _seller():
    import json
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as fh:
        return json.load(fh).get("seller", {})


@accounts_bp.before_request
@auth.login_required
def _require_login():
    pass


@accounts_bp.route("/")
def dashboard():
    dash, exec_sum, rem, seg_cards = {}, {}, {}, {}
    seg_id = auth.user_segment_id(auth.current_user())
    try:
        dash = ls.outstanding_dashboard()
    except Exception as exc:  # noqa: BLE001
        flash(f"Database: {exc}")
    try:
        import ar_service as ar
        import segment_service as seg
        exec_sum = ar.executive_summary()
        rem = ar.reminder_dashboard()
        seg_cards = seg.executive_segment_cards()
        if seg_id:
            seg_cards["segments"] = [s for s in seg_cards.get("segments", [])
                                     if s["BusinessSegmentId"] == seg_id]
    except Exception:  # noqa: BLE001
        pass
    return render_template("accounts/dashboard.html", dash=dash, exec_sum=exec_sum,
                           rem=rem, seg_cards=seg_cards, user=auth.current_user())


@accounts_bp.route("/invoices")
def invoices():
    client_id = request.args.get("client_id", type=int)
    if "segment_id" in request.args:
        seg_arg = request.args.get("segment_id", "")
        seg_id = int(seg_arg) if seg_arg else None
    else:
        seg_id = auth.user_segment_id(auth.current_user())
    q = (request.args.get("q") or "").strip()
    archive_path = ""
    try:
        rows = ls.list_invoices(client_id, segment_id=seg_id)
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r.get("ClientName") or "").lower()
                    or ql in str(r.get("InvoiceNumber") or "").lower()]
        out_folder = inf.write_folder()
        archive_path = out_folder
        rows = inf.enrich_for_archive(rows)
        import segment_service as seg
        segments = seg.list_segments()
        clients = ls.list_clients()
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        rows, clients, segments = [], [], []
    return render_template("accounts/invoices.html", invoices=rows,
                           clients=clients, client_id=client_id,
                           segments=segments, segment_id=seg_id,
                           search_q=q, archive_path=archive_path,
                           user=auth.current_user(),
                           can_edit_invoices=_can_edit_invoices())


@accounts_bp.route("/invoices/<int:invoice_id>/download/<fmt>")
def invoice_download(invoice_id, fmt):
    if fmt not in ("pdf", "excel"):
        flash("Invalid format.")
        return redirect(url_for("accounts.invoices"))
    try:
        inv = ls.get_invoice(invoice_id)
        if not inv:
            flash("Invoice not found.")
            return redirect(url_for("accounts.invoices"))
        seg_id = auth.user_segment_id(auth.current_user())
        if seg_id and inv.get("BusinessSegmentId") != seg_id:
            flash("You cannot access invoices from other segments.")
            return redirect(url_for("accounts.invoices"))
        path = inf.ensure_download(inv, fmt)
        return send_file(path, as_attachment=True,
                         download_name=os.path.basename(path))
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
    return redirect(url_for("accounts.invoices"))


def _can_edit_invoices() -> bool:
    return bool(auth.current_user())


def _assert_can_edit_invoice(inv: dict) -> None:
    if not inv:
        raise ValueError("Invoice not found.")
    seg_id = auth.user_segment_id(auth.current_user())
    if seg_id and inv.get("BusinessSegmentId") and int(inv["BusinessSegmentId"]) != int(seg_id):
        raise PermissionError("You cannot edit invoices from other segments.")


def _line_items_for_edit(invoice_id: int, inv: dict) -> list[dict]:
    items = ls.get_invoice_line_items(invoice_id)
    if items:
        return items
    amt = float(inv.get("TaxableAmount") or 0) or float(inv.get("TotalAmount") or 0)
    return [{
        "Particulars": "As per invoice",
        "WorkDate": inv.get("InvoiceDate"),
        "Amount": amt,
    }]


@accounts_bp.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def invoice_edit(invoice_id):
    if not _can_edit_invoices():
        flash("Only Admin or Accounts can edit invoice details.")
        return redirect(url_for("accounts.invoices"))
    inv = ls.get_invoice(invoice_id)
    if not inv:
        flash("Invoice not found.")
        return redirect(url_for("accounts.invoices"))
    try:
        _assert_can_edit_invoice(inv)
    except PermissionError as exc:
        flash(str(exc))
        return redirect(url_for("accounts.invoices"))
    import segment_service as seg
    segments = seg.list_segments()
    clients = ls.list_clients()
    line_items = _line_items_for_edit(invoice_id, inv)

    if request.method == "POST":
        try:
            # --- Core fields ---
            new_seg = int(request.form.get("segment_id", 0))
            if not new_seg:
                raise ValueError("Please select a business segment.")
            inv_date = (request.form.get("invoice_date") or "").strip() or None
            new_client_id = int(request.form.get("client_id") or inv["ClientId"])
            supply_type = (request.form.get("supply_type") or "").strip() or None
            new_inv_no = (request.form.get("invoice_number") or "").strip() or str(inv["InvoiceNumber"])

            # --- Line items ---
            raw_parts = request.form.getlist("particulars[]")
            raw_dates = request.form.getlist("item_date[]")
            raw_amts  = request.form.getlist("amount[]")
            new_items = []
            for part, idate, amt_s in zip(raw_parts, raw_dates, raw_amts):
                part = part.strip()
                amt_s = amt_s.strip()
                if not part and not amt_s:
                    continue
                if not part:
                    raise ValueError("A row has an amount but no particulars.")
                try:
                    amt = float(amt_s)
                except ValueError:
                    raise ValueError(f"Invalid amount for: {part!r}")
                if amt < 0:
                    raise ValueError(f"Amount cannot be negative for: {part!r}")
                new_items.append({"particulars": part,
                                  "date": idate.strip(),
                                  "amount": amt})
            if not new_items:
                raise ValueError("Add at least one line item with an amount.")

            # --- Recalculate totals from line items ---
            subtotal = sum(it["amount"] for it in new_items)
            # Determine tax type from client's state (MhState)
            client_row = ls.get_client(new_client_id)
            mh = bool(client_row.get("MhState")) if client_row else False
            cgst = sgst = igst = 0.0
            if supply_type == "IGST":
                igst = round(subtotal * 0.18, 2)
                mh = False
            elif supply_type in ("CGST_SGST", ""):
                cgst = round(subtotal * 0.09, 2)
                sgst = round(subtotal * 0.09, 2)
            else:
                # Auto-detect from client
                if mh:
                    cgst = round(subtotal * 0.09, 2)
                    sgst = round(subtotal * 0.09, 2)
                    supply_type = "CGST_SGST"
                else:
                    igst = round(subtotal * 0.18, 2)
                    supply_type = "IGST"
            total = round(subtotal + cgst + sgst + igst, 2)

            # --- Save to DB ---
            ls.update_invoice(
                invoice_id,
                segment_id=new_seg,
                invoice_date=inv_date,
                client_id=new_client_id,
                taxable_amount=subtotal,
                cgst_amount=cgst,
                sgst_amount=sgst,
                igst_amount=igst,
                total_amount=total,
                supply_type=supply_type,
                line_items=new_items,
                invoice_number=new_inv_no,
            )

            do_regen = request.form.get("regenerate") == "1" or request.form.get("regenerate") == "on"
            if do_regen:
                try:
                    saved = ls.get_invoice(invoice_id) or inv
                    paths = inf.rebuild(saved)
                    ls.update_invoice(
                        invoice_id,
                        pdf_path=paths.get("pdf") or "",
                        excel_path=paths.get("excel") or "",
                    )
                    flash(f"Invoice {new_inv_no} updated and PDF/Excel regenerated.")
                except Exception as regen_exc:  # noqa: BLE001
                    flash(f"Saved in accounts, but file regeneration failed: {regen_exc}")
            else:
                flash(f"Invoice {new_inv_no} updated (database only — files not regenerated).")

            import audit as audit_mod
            audit_mod.log(
                auth.current_user(), "edit_invoice", "invoice",
                str(inv.get("InvoiceNumber")),
                f"full_edit total={total}")
            return redirect(url_for("accounts.invoices"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc))

    return render_template(
        "accounts/invoice_edit.html",
        invoice=inv,
        line_items=line_items,
        segments=segments,
        clients=clients,
        user=auth.current_user())


@accounts_bp.route("/invoices/<int:invoice_id>/convert-to-tax", methods=["POST"])
def invoice_convert_to_tax(invoice_id):
    if not _can_edit_invoices():
        flash("Access denied.")
        return redirect(url_for("accounts.invoices"))
    inv = ls.get_invoice(invoice_id)
    if not inv:
        flash("Invoice not found.")
        return redirect(url_for("accounts.invoices"))
    try:
        _assert_can_edit_invoice(inv)
    except PermissionError as exc:
        flash(str(exc))
        return redirect(url_for("accounts.invoices"))
    if (inv.get("InvoiceType") or "TAX").upper() != "PROFORMA":
        flash("Only proforma invoices can be converted to tax invoices.")
        return redirect(url_for("accounts.invoices"))

    import re as _re
    import json as _json
    from generator import generate_invoice
    from pdf_generator import generate_pdf_invoice
    from invoice_core import next_invoice_number
    import audit as audit_mod

    try:
        config = inf.load_config()
        out_folder = inf.write_folder()
        os.makedirs(out_folder, exist_ok=True)

        line_items = ls.get_invoice_line_items(invoice_id)
        items = [{"particulars": it.get("Particulars") or "",
                  "date": inf._fmt_date(it.get("WorkDate")),
                  "amount": float(it.get("Amount") or 0)} for it in line_items]

        client_row = ls.get_client(inv["ClientId"]) or {}
        import clients as clients_mod
        client_obj = clients_mod.Client(
            code=str(inv["ClientId"]),
            name=client_row.get("ClientName") or inv["ClientName"],
            gstin=client_row.get("GSTIN") or "",
            mh=bool(client_row.get("MhState")),
            address=client_row.get("Address") or "",
        )

        tax_no = str(next_invoice_number(config, out_folder))

        def _safe(t):
            t = _re.sub(r"[^A-Za-z0-9 _.-]", "", t or "").strip()
            return _re.sub(r"\s+", "_", t)[:60] or "invoice"

        base = f"Invoice_{_safe(tax_no)}_{_safe(client_obj.name)}"
        inv_date = inf._fmt_date(inv.get("InvoiceDate"))
        xlsx_path = os.path.join(out_folder, base + ".xlsx")
        pdf_path = os.path.join(out_folder, base + ".pdf")
        generate_invoice(config, client_obj, tax_no, inv_date, items, xlsx_path, document_type="tax")
        generate_pdf_invoice(config, client_obj, tax_no, inv_date, items, pdf_path, document_type="tax")

        import db as _db
        with _db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE dbo.TaxInvoices SET
                   InvoiceNumber=?, InvoiceType=N'TAX',
                   PdfPath=?, ExcelPath=?, ConvertedFromInvoiceId=?
                   WHERE InvoiceId=?""",
                tax_no, pdf_path, xlsx_path, invoice_id, invoice_id)
            conn.commit()

        try:
            if int(tax_no) >= config["invoice"]["next_number"]:
                config["invoice"]["next_number"] = int(tax_no) + 1
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    _json.dump(config, fh, indent=2, ensure_ascii=False)
        except ValueError:
            pass

        audit_mod.log(auth.current_user(), "convert_proforma", "invoice",
                      str(tax_no), f"from={inv.get('InvoiceNumber')}")
        flash(f"Proforma {inv['InvoiceNumber']} converted to Tax Invoice {tax_no}. "
              f"Now posted to ledger & outstanding.")
    except Exception as exc:  # noqa: BLE001
        flash(f"Conversion failed: {exc}")
    return redirect(url_for("accounts.invoices"))


@accounts_bp.route("/non-gst", methods=["GET", "POST"])
def non_gst_bills():
    import segment_service as seg
    import audit as audit_mod
    if request.method == "POST":
        action = request.form.get("action", "add")
        try:
            if action == "delete":
                bid = int(request.form["bill_id"])
                ok, msg = ls.delete_non_gst_bill(bid)
                if ok:
                    audit_mod.log(auth.current_user(), "delete_non_gst", "nongst", str(bid))
                flash(msg)
                return redirect(url_for("accounts.non_gst_bills"))
            if action == "edit":
                bid = int(request.form["bill_id"])
                ls.update_non_gst_bill(
                    bid,
                    amount=float(request.form.get("amount", 0)),
                    description=request.form.get("description") or "",
                    bill_date=request.form.get("bill_date") or None,
                    segment_id=int(request.form.get("segment_id", 1)),
                    remarks=request.form.get("remarks") or "",
                )
                audit_mod.log(auth.current_user(), "edit_non_gst", "nongst", str(bid))
                flash("Non-GST bill updated.")
                return redirect(url_for("accounts.non_gst_bills"))
            cid = int(request.form.get("client_id", 0))
            amt = float(request.form.get("amount", 0))
            bid = ls.create_non_gst_bill(
                cid,
                request.form.get("bill_date") or "",
                amt,
                request.form.get("description") or "",
                segment_id=int(request.form.get("segment_id", 1)),
                remarks=request.form.get("remarks") or "",
                created_by=auth.current_user())
            bill = ls.get_non_gst_bill(bid)
            audit_mod.log(auth.current_user(), "add_non_gst", "nongst", str(bid))
            flash(f"Non-GST bill {bill['BillNumber']} saved — ₹ {amt:,.2f}")
            return redirect(url_for("accounts.non_gst_bills"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc))
    client_id = request.args.get("client_id", type=int)
    if "segment_id" in request.args:
        seg_arg = request.args.get("segment_id", "")
        seg_id = int(seg_arg) if seg_arg else None
    else:
        seg_id = auth.user_segment_id(auth.current_user())
    edit_id = request.args.get("edit", type=int)
    try:
        rows = ls.list_non_gst_bills(client_id, segment_id=seg_id)
        clients = ls.list_clients()
        segments = seg.list_segments()
        next_bill = ls.peek_non_gst_bill_number()
        edit_bill = ls.get_non_gst_bill(edit_id) if edit_id else None
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        rows, clients, segments, next_bill, edit_bill = [], [], [], "NGB-00001", None
    return render_template(
        "accounts/non_gst_bills.html",
        bills=rows, clients=clients, segments=segments,
        client_id=client_id, segment_id=seg_id,
        next_bill=next_bill, edit_bill=edit_bill,
        today=date.today().strftime("%d-%m-%Y"),
        user=auth.current_user())


@accounts_bp.route("/receipts", methods=["GET", "POST"])
def receipts():
    import ar_service as ar
    import audit as audit_mod
    if request.method == "POST":
        action = request.form.get("action", "add")
        try:
            if action == "delete":
                rid = int(request.form["receipt_id"])
                ok, msg = ls.delete_receipt(rid)
                if ok:
                    audit_mod.log(auth.current_user(), "delete_receipt", "receipt", str(rid))
                flash(msg)
                return redirect(url_for("accounts.receipts"))

            cid = int(request.form.get("client_id", 0))
            amt = float(request.form.get("amount", 0))
            if amt <= 0:
                raise ValueError("Amount must be positive.")
            taxable = float(request.form.get("taxable_amount") or request.form.get("invoice_amount") or 0)
            tds_pct = float(request.form.get("tds_percent") or 0)
            tds_manual = request.form.get("tds_manual") == "1"
            tds_amt_manual = request.form.get("tds_amount")
            tds_amt_val = float(tds_amt_manual) if tds_amt_manual not in (None, "") else None
            if not tds_manual:
                tds_amt_val = None
            gst_amount = float(request.form.get("gst_amount") or 0)
            gst_paid = float(request.form.get("gst_paid_amount") or 0)
            gst_status = request.form.get("gst_paid_status") or "unknown"
            inv_ids = None
            raw_ids = request.form.getlist("invoice_ids")
            if raw_ids:
                inv_ids = [int(x) for x in raw_ids if x]

            common = dict(
                client_id=cid,
                receipt_date=request.form.get("receipt_date") or "",
                amount_received=amt,
                payment_mode=request.form.get("payment_mode") or "NEFT",
                taxable_amount=taxable,
                tds_pct=tds_pct,
                tds_manual=tds_manual,
                tds_amount_manual=tds_amt_val,
                reference=request.form.get("reference") or "",
                remarks=request.form.get("remarks") or "",
                invoice_ids=inv_ids,
                gst_amount=gst_amount,
                gst_paid_amount=gst_paid,
                gst_paid_status=gst_status,
            )

            if action == "edit":
                rid = int(request.form["receipt_id"])
                ar.update_receipt_with_tds(
                    rid,
                    receipt_number=(request.form.get("receipt_number") or None),
                    **common)
                audit_mod.log(auth.current_user(), "edit_receipt", "receipt", str(rid))
                flash("Receipt updated.")
                return redirect(url_for("accounts.receipts"))

            rnum = ar.add_receipt_with_tds(
                **common,
                invoice_amount=taxable,
                receipt_number=(request.form.get("receipt_number") or None),
                created_by=auth.current_user())
            audit_mod.log(auth.current_user(), "add_receipt", "receipt", rnum)
            flash(f"Receipt {rnum} recorded successfully.")
            return redirect(url_for("accounts.receipts"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc))

    edit_id = request.args.get("edit", type=int)
    client_id = request.args.get("client_id", type=int)
    edit_receipt = None
    edit_alloc = {"invoice_ids": []}
    rows, clients, next_rcp = [], [], "RCP-00001"
    try:
        rows = ls.list_receipts(client_id)
        clients = ls.list_clients()
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
    try:
        next_rcp = ls.next_receipt_number()
    except Exception:
        next_rcp = "RCP-00001"
    try:
        if edit_id:
            edit_receipt = ls.get_receipt(edit_id)
            if edit_receipt:
                edit_alloc = ls.get_receipt_allocations(edit_id)
    except Exception:
        pass
    return render_template(
        "accounts/receipts.html",
        receipts=rows, clients=clients, next_receipt=next_rcp,
        edit_receipt=edit_receipt, edit_alloc=edit_alloc,
        today=date.today().strftime("%d-%m-%Y"),
        user=auth.current_user())


@accounts_bp.route("/api/client/<int:client_id>/open-invoices")
def api_client_open_invoices(client_id):
    try:
        exclude_id = request.args.get("receipt_id", type=int)
        rows = ls.client_open_invoices(client_id, exclude_receipt_id=exclude_id)
        out = []
        for r in rows:
            out.append({
                "InvoiceId": r["InvoiceId"],
                "InvoiceNumber": r["InvoiceNumber"],
                "Pending": float(r.get("Pending") or 0),
                "TaxableAmount": float(r.get("TaxableAmount") or 0),
                "TotalAmount": float(r.get("TotalAmount") or 0),
            })
        return jsonify({"ok": True, "invoices": out})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 500


@accounts_bp.route("/ledger", methods=["GET"])
def ledger():
    client_id = request.args.get("client_id", type=int)
    from_date = request.args.get("from_date", "")
    to_date = request.args.get("to_date", "")
    seg_arg = request.args.get("segment_id", "")
    seg_id = int(seg_arg) if seg_arg else auth.user_segment_id(auth.current_user())
    ledger_rows, client, summary = [], None, {}
    try:
        clients = ls.list_clients()
        import segment_service as seg
        segments = seg.list_segments()
        if client_id:
            client = ls.get_client(client_id)
            summary = ls.client_summary(client_id) or {}
            ledger_rows = ls.client_ledger(client_id, from_date or None, to_date or None,
                                           segment_id=seg_id)
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        clients, segments = [], []
    return render_template("accounts/ledger.html", clients=clients,
                           client_id=client_id, client=client,
                           summary=summary, ledger=ledger_rows,
                           from_date=from_date, to_date=to_date,
                           segments=segments, segment_id=seg_id,
                           user=auth.current_user())


@accounts_bp.route("/ledger/export/<fmt>")
def ledger_export(fmt):
    client_id = request.args.get("client_id", type=int)
    if not client_id:
        flash("Select a client first.")
        return redirect(url_for("accounts.ledger"))
    try:
        client = ls.get_client(client_id)
        summary = ls.client_summary(client_id) or {}
        rows = ls.client_ledger(client_id,
                                request.args.get("from_date") or None,
                                request.args.get("to_date") or None)
        safe = client["ClientName"].replace(" ", "_")[:40]
        os.makedirs(EXPORT_DIR, exist_ok=True)
        if fmt == "pdf":
            path = os.path.join(EXPORT_DIR, f"Ledger_{safe}.pdf")
            export_ledger_pdf(client, summary, rows, _seller(), path)
            return send_file(path, as_attachment=True)
        if fmt == "excel":
            path = os.path.join(EXPORT_DIR, f"Ledger_{safe}.xlsx")
            export_ledger_excel(client, summary, rows, _seller(), path)
            return send_file(path, as_attachment=True)
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
    return redirect(url_for("accounts.ledger", client_id=client_id))


@accounts_bp.route("/outstanding")
def outstanding():
    seg_id = auth.user_segment_id(auth.current_user())
    try:
        rows = ls.client_outstanding_list()
        dash = ls.outstanding_dashboard()
        import segment_service as seg
        seg_rows = ls.segment_outstanding_list(seg_id)
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        rows, dash, seg_rows = [], {}, []
    return render_template("accounts/outstanding.html", clients=rows, dash=dash,
                           segment_outstanding=seg_rows, user=auth.current_user())


@accounts_bp.route("/ageing")
def ageing():
    try:
        buckets = ls.ageing_analysis()
        detail = ls.ageing_detail()
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        buckets, detail = [], []
    return render_template("accounts/ageing.html", buckets=buckets, detail=detail,
                           user=auth.current_user())


@accounts_bp.route("/client/<int:client_id>")
def client_detail(client_id):
    try:
        client = ls.get_client(client_id)
        summary = ls.client_summary(client_id) or {}
        invoices = ls.list_invoices(client_id, limit=20,
                                    segment_id=auth.user_segment_id(auth.current_user()))
        non_gst = ls.list_non_gst_bills(client_id, limit=20)
        receipts = ls.list_receipts(client_id, limit=20)
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        return redirect(url_for("accounts.outstanding"))
    return render_template("accounts/client_detail.html", client=client,
                           summary=summary, invoices=invoices,
                           non_gst_bills=non_gst, receipts=receipts,
                           user=auth.current_user())


@accounts_bp.route("/clients", methods=["GET", "POST"])
def clients_master():
    if request.method == "POST":
        action = request.form.get("action", "save")
        try:
            import clients as clients_mod
            import audit as audit_mod
            if action == "add":
                name = (request.form.get("client_name") or "").strip()
                mh = request.form.get("mh_state") == "1"
                cid = ls.create_client(
                    name,
                    request.form.get("gstin") or "",
                    request.form.get("address") or "",
                    request.form.get("contact_person") or "",
                    request.form.get("email") or "",
                    request.form.get("mobile") or "",
                    mh=mh,
                    opening_balance=float(request.form.get("opening_balance") or 0))
                clients_mod.save_custom_client(
                    name, request.form.get("gstin") or "",
                    request.form.get("address") or "", mh)
                audit_mod.log(auth.current_user(), "add_client", "client", str(cid), name)
                flash(f"Client '{name}' added.")
                return redirect(url_for("accounts.clients_master"))
            if action == "delete":
                cid = int(request.form.get("client_id", 0))
                client = ls.get_client(cid)
                ok, msg = ls.delete_client(cid)
                if client:
                    clients_mod.remove_custom_client(client["ClientName"])
                if ok:
                    audit_mod.log(auth.current_user(), "delete_client", "client", str(cid))
                flash(msg)
                return redirect(url_for("accounts.clients_master"))
            cid = int(request.form.get("client_id", 0))
            old = ls.get_client(cid)
            new_name = (request.form.get("client_name") or "").strip()
            mh = request.form.get("mh_state") == "1"
            ls.update_client(
                cid,
                ClientName=new_name or None,
                ContactPerson=request.form.get("contact_person") or None,
                Email=request.form.get("email") or None,
                Mobile=request.form.get("mobile") or None,
                Address=request.form.get("address") or None,
                GSTIN=request.form.get("gstin") or None,
                MhState=1 if mh else 0,
                OpeningBalance=float(request.form.get("opening_balance") or 0),
            )
            if old and new_name and old["ClientName"] != new_name:
                clients_mod.rename_custom_client(old["ClientName"], new_name)
            audit_mod.log(auth.current_user(), "update_client", "client", str(cid), new_name)
            flash("Client updated.")
            return redirect(url_for("accounts.clients_master"))
        except Exception as exc:  # noqa: BLE001
            flash(str(exc))
    try:
        rows = ls.list_clients()
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
        rows = []
    edit_id = request.args.get("edit", type=int)
    edit_client = ls.get_client(edit_id) if edit_id else None
    usage = ls.client_usage(edit_id) if edit_id else {}
    return render_template("accounts/clients.html", clients=rows,
                           edit_client=edit_client, usage=usage,
                           user=auth.current_user())


@accounts_bp.route("/clients/sync", methods=["POST"])
def sync_clients():
    try:
        import json
        import clients as clients_mod
        cfg_path = os.path.join(HERE, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        wb_rel = config["paths"]["clients_workbook"]
        wb_path = wb_rel if os.path.isabs(wb_rel) else os.path.join(HERE, wb_rel)
        client_list, msg = clients_mod.load_clients(
            wb_path, config["paths"]["clients_sheet"])
        n = ls.sync_clients_from_workbook(client_list)
        flash(f"Synced {n} clients from workbook. ({msg})")
    except Exception as exc:  # noqa: BLE001
        flash(str(exc))
    return redirect(request.referrer or url_for("accounts.dashboard"))


# ─── Bank Statement Import ────────────────────────────────────────────────────

@accounts_bp.route("/bank-import", methods=["GET", "POST"])
def bank_import():
    if not _can_edit_invoices():
        flash("Only Admin or Accounts users can import bank statements.")
        return redirect(url_for("accounts.receipts"))

    if request.method == "POST":
        f = request.files.get("statement")
        if not f or not f.filename:
            flash("Please select a file.")
            return redirect(url_for("accounts.bank_import"))
        ext = os.path.splitext(f.filename.lower())[1]
        if ext not in (".xlsx", ".xls", ".csv", ".pdf"):
            flash("Supported formats: .xlsx  .xls  .csv  .pdf")
            return redirect(url_for("accounts.bank_import"))
        try:
            import bank_import as bi
            file_bytes = f.read()
            bank_name, raw_rows = bi.parse_statement(file_bytes, f.filename)
            if not raw_rows:
                flash("No credit transactions found. Check the file format or column layout.")
                return redirect(url_for("accounts.bank_import"))

            clients = ls.list_clients()
            matched  = bi.match_clients(raw_rows, clients)
            with_dups = bi.find_duplicates(matched)

            # Compute TDS/GST suggestions per row
            enriched = []
            for row in with_dups:
                sugg = bi.suggest_tds_gst(row.get("matched_client_id"), float(row["amount"]))
                enriched.append({**row, **sugg})

            token = bi.save_parsed(enriched)
            dup_count = sum(1 for r in enriched if r["is_duplicate"])
            unmatched = sum(1 for r in enriched if not r["matched_client_id"])
            return render_template(
                "accounts/bank_import_preview.html",
                rows=enriched,
                clients=clients,
                bank_name=bank_name,
                token=token,
                filename=f.filename,
                dup_count=dup_count,
                unmatched=unmatched,
                user=auth.current_user(),
            )
        except Exception as exc:  # noqa: BLE001
            flash(f"Parse error: {exc}")
            return redirect(url_for("accounts.bank_import"))

    return render_template("accounts/bank_import.html", user=auth.current_user())


@accounts_bp.route("/bank-import/confirm", methods=["POST"])
def bank_import_confirm():
    if not _can_edit_invoices():
        flash("Access denied.")
        return redirect(url_for("accounts.receipts"))

    import bank_import as bi
    import ar_service as ar
    import audit as audit_mod
    import db as _db

    token    = request.form.get("token", "")
    filename = request.form.get("filename", "")
    bank_nm  = request.form.get("bank_name", "")
    row_count = int(request.form.get("row_count", 0))
    all_rows  = bi.load_parsed(token)

    imported = skipped = errors = 0

    for idx in range(row_count):
        if f"import_{idx}" not in request.form:
            skipped += 1
            continue
        try:
            client_id = int(request.form.get(f"client_id_{idx}", 0) or 0)
            if not client_id:
                skipped += 1
                continue

            receipt_date  = (request.form.get(f"date_{idx}") or "").strip()
            amount        = float(request.form.get(f"amount_{idx}") or 0)
            tds_pct       = float(request.form.get(f"tds_pct_{idx}") or 0)
            gst_paid      = request.form.get(f"gst_paid_{idx}") == "1"
            gst_rate      = float(request.form.get(f"gst_rate_{idx}") or 0)
            taxable       = float(request.form.get(f"taxable_{idx}") or amount)
            payment_mode  = request.form.get(f"pmode_{idx}") or "NEFT"
            reference     = (request.form.get(f"ref_{idx}") or "").strip()
            narr          = all_rows[idx]["narration"] if idx < len(all_rows) else ""

            gst_amt = round(taxable * gst_rate / 100, 2) if gst_paid else 0.0
            gst_status = "full" if gst_paid and gst_amt > 0 else ("none" if gst_rate else "unknown")

            remarks_parts = [f"Bank Import ({bank_nm})", narr[:150]]
            if gst_paid and gst_amt:
                remarks_parts.append(f"GST {gst_rate}%=₹{gst_amt:,.2f} paid by client")
            elif gst_rate:
                remarks_parts.append(f"GST {gst_rate}% NOT paid by client")
            remarks = " | ".join(p for p in remarks_parts if p)

            ar.add_receipt_with_tds(
                client_id=client_id,
                receipt_date=receipt_date,
                amount_received=amount,
                payment_mode=payment_mode,
                taxable_amount=taxable,
                invoice_amount=taxable,
                tds_pct=tds_pct,
                reference=reference,
                remarks=remarks,
                created_by=auth.current_user(),
                gst_amount=gst_amt if gst_rate else 0,
                gst_paid_amount=gst_amt if gst_paid else 0,
                gst_paid_status=gst_status,
            )
            audit_mod.log(auth.current_user(), "bank_import_receipt", "receipt",
                          reference or str(idx),
                          f"client={client_id} amount={amount} tds={tds_pct}% gst_paid={gst_paid}")
            imported += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            flash(f"Row {idx + 1}: {exc}")

    # Log the batch
    try:
        with _db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """IF OBJECT_ID(N'dbo.BankImportLog','U') IS NOT NULL
                   INSERT INTO dbo.BankImportLog
                   (FileName,BankDetected,ImportedBy,RowsTotal,RowsImported,RowsSkipped,RowsError)
                   VALUES (?,?,?,?,?,?,?)""",
                filename, bank_nm, auth.current_user(),
                row_count, imported, skipped, errors)
            conn.commit()
    except Exception:  # noqa: BLE001
        pass

    bi.delete_parsed(token)
    flash(f"Bank import complete — {imported} receipt(s) created, "
          f"{skipped} skipped, {errors} error(s).")
    return redirect(url_for("accounts.receipts"))


# FSS Invoice & Accounts Suite

One integrated application for **Façade Structural Services**:

- **Tax Invoices** — PDF & Excel generation with GST (CGST/SGST/IGST)
- **Client Ledger** — Tally-style debit/credit, outstanding, ageing
- **Payment Receipts** — with TDS tracking
- **Email & WhatsApp reminders**
- **GST receivable** & **TDS** reports
- **Profitability** & **Executive** dashboards
- **Team logins** with roles (Admin, Accounts, Management, Viewer)

Backed by **SQL Server** (`FSSInvoice` database).

---

## Quick start

1. **Install once:** `pip install -r requirements.txt`
2. **Database:** double-click `Setup Database.bat`
3. **Run app:** double-click `Run Web Invoice Tool.bat` or Desktop shortcut **FSS Invoice Tool**
4. **First login:** create your username & password on the setup screen
5. **Network (team):** run `Allow on Network (Run as Admin).bat`, share URL from `Show Network Address.bat`

### Go live (24/7)

| Goal | What to run |
|------|-------------|
| **Office team (same Wi‑Fi/LAN)** | `Go Live.bat` → share `http://YOUR-PC-IP:5000` |
| **Always on after PC restart** | `Go Live.bat` (creates Windows startup task) |
| **Internet (work from home)** | Keep server running + `Go Live - Internet (Cloudflare).bat` |

Production server uses **Waitress** (`run_live.py`), not the Flask debug server.

### STAAD diagram PDF reports

Separate utility in [`staad_report_tool/`](staad_report_tool/README.md): generate FSS-branded diagram PDFs from STAAD `.std` files (not linked to invoicing). Run `staad_report_tool\Generate STAAD Report.bat` or see that README.

---

## Main screens

| Screen | Path | Purpose |
|--------|------|---------|
| **Tax Invoices** | Home `/` | Create invoices |
| **Accounts Hub** | `/accounts/` | Dashboard + links to all modules |
| **Client Master** | `/accounts/clients` | Clients, GSTIN, contacts |
| **Receipts** | `/accounts/receipts` | Payments + TDS |
| **Client Ledger** | `/accounts/ledger` | Running balance, PDF/Excel export |
| **Reminders** | `/accounts/reminders` | Email payment reminders |
| **WhatsApp** | `/accounts/whatsapp` | Outstanding messages |
| **TDS / GST** | `/accounts/tds`, `/accounts/gst` | Compliance reports |
| **Executive** | `/accounts/executive` | Management dashboard |
| **Profitability** | `/accounts/profitability` | Client margins |
| **Team** | `/users` | User accounts (admin) |

Every generated invoice is **automatically posted** to the SQL ledger.

---

## Configuration (`config.json`)

- `database` — SQL Server connection
- `email` — SMTP for automatic reminders
- `reminders.payment_terms_days` — invoice due date (default 30 days)
- `server` — host/port for team access (`0.0.0.0` = network)

---

## SQL scripts

`database/01` … `07` — run via `Setup Database.bat`  
See `database/AR_MODULES.md` and `database/README.md` for details.

---

## Backup

`Backup Database.bat` → `C:\FSS_Backups\`

---

## Files

| File | Role |
|------|------|
| `launch.pyw` | Desktop launcher (hidden server + browser) |
| `web.py` | Main Flask application |
| `accounts.py` + `ar_routes.py` | Accounts UI routes |
| `ledger_service.py` + `ar_service.py` | Business logic |
| `generator.py` + `pdf_generator.py` | Invoice documents |
| `db.py` | Database migration |

# 24x7 live (office PC band ho to bhi)

**Sabse aasan (card nahi):** [Supabase](SUPABASE.md) — `Setup Supabase.bat`

Office PC + Cloudflare **hamesha ON nahi** reh sakta. Permanent live:

**Azure SQL (FREE) + existing Vercel URL**  
https://fss-invoice-tool.vercel.app

1. Double-click **`Always Live.bat`**
2. Browser mein Azure SQL **Start free** se database `FSSInvoice` banao
3. SQL login + password yaad rakho; firewall: **Allow Azure services = ON**
4. Bat window mein server / user / password daalo (local data copy ho jayegi)
5. Vercel → Settings → Environment Variables mein 7 values paste + **Redeploy**

Azure SQL free: https://aka.ms/azuresqlhub

---

Yeh app **Flask + SQL Server** par bani hai. Vercel par chalane ke liye **cloud database (Azure SQL)** chahiye — local `(local)` SQL Server Vercel se connect nahi hoga.

## Azure SQL Database (free) — details

1. [Azure Portal](https://portal.azure.com) → **Create SQL Database**
2. Server name note karein: e.g. `fss-invoice.database.windows.net`
3. Database name: `FSSInvoice`
4. SQL login + password banayein
5. **Firewall** → **Allow Azure services** ON
6. Apne IP ko bhi allow karein (pehli migration ke liye)

---

## Step 2 — Database schema apply karein (ek baar)

Apne PC par (SQL connected):

```powershell
cd "C:\FSS Invoice Tool"
# config.json mein Azure server details daalein, phir:
python -c "import db; print(db.migrate())"
```

Ya Azure Portal → Query editor se `database/02_tables.sql` … `12_receipts_proforma.sql` run karein.

---

## Step 3 — GitHub se Vercel connect

1. [vercel.com](https://vercel.com) → Login → **Add New Project**
2. Import: **https://github.com/sanjaybzpal-code/FSS-Invoice-Tool-**
3. Framework: **Flask** (auto-detect)
4. Root directory: `./` (default)

---

## Step 4 — Vercel Environment Variables

Vercel Project → **Settings → Environment Variables** — yeh add karein:

| Variable | Example | Required |
|----------|---------|----------|
| `AZURE_SQL_HOST` | `fss-invoice.database.windows.net` | Yes |
| `AZURE_SQL_USER` | `fssadmin` | Yes |
| `AZURE_SQL_PASSWORD` | `YourStrongPassword123!` | Yes |
| `AZURE_SQL_DATABASE` | `FSSInvoice` | Yes |
| `FLASK_SECRET_KEY` | random 64-char string | Yes |
| `ADMIN_USERNAME` | `admin` | Yes |
| `ADMIN_PASSWORD` | your login password | Yes |

Optional — poora config JSON (seller details, GST rates):

| Variable | Value |
|----------|-------|
| `CONFIG_JSON` | One-line JSON from `config.json` (seller, tax, etc.) |

---

## Step 5 — Deploy

Click **Deploy**. URL milega: `https://your-project.vercel.app`

Login: `ADMIN_USERNAME` + `ADMIN_PASSWORD`

---

## Important limitations on Vercel

| Item | Note |
|------|------|
| **PDF/Excel files** | `/tmp` par save — har cold start par files gayab ho sakti hain. Permanent storage ke liye Azure Blob add karna padega. |
| **SQL Server local** | Kaam nahi karega — sirf **Azure SQL** |
| **Timeout** | Lambi reports 60 sec limit (Pro plan par badha sakte hain) |
| **Team LAN** | Office ke liye `Go Live.bat` zyada simple hai |

---

## Agar Vercel par error aaye

1. **Build failed** → `requirements.txt` check karein
2. **500 / crash on load** → `pyproject.toml` must list dependencies (Vercel uses uv, not only `requirements.txt`). Redeploy after git push.
3. **Database error** → Azure firewall + env variables check karein
3. **Login nahi ho raha** → `ADMIN_USERNAME` / `ADMIN_PASSWORD` set karein

---

## Alternative (aasan)

Office PC par **`Go Live.bat`** + **Cloudflare Tunnel** — bina Azure SQL ke, local database ke saath internet par share kar sakte hain.

---

*Deploy ke baad URL client ko bhej dein — login required hai.*

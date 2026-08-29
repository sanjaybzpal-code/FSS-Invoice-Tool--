# Supabase (24×7 live, Azure card nahi)

Office PC par SQL Server **waise hi** chalta hai.  
Internet / Vercel par **Supabase Postgres** use hota hai.

## Ek baar setup

1. [supabase.com](https://supabase.com) → GitHub se login → **New project**
2. **Settings → Database → Connection string → URI** copy karo  
   Password `[YOUR-PASSWORD]` hata ke asli DB password daalo
3. Is folder mein **`Setup Supabase.bat`** chalao aur URI paste karo  
   (local invoices/receipts Supabase par copy ho jayenge)
4. Vercel → **Environment Variables**:
   - `SUPABASE_DB_URL` = wahi URI
   - `FLASK_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`
5. **Redeploy**

Client: **https://fss-invoice-tool.vercel.app**  
PC OFF ho to bhi data Supabase mein rahega.

Naye office invoices ke baad `Setup Supabase.bat` dubara chalao taaki cloud update ho.

URI kabhi GitHub par commit mat karna.

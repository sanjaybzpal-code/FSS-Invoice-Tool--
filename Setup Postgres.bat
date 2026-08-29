@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
title FSS — Setup PostgreSQL
echo.
echo Host 43.205.3.136  Port 5432  Database fss_invoice
echo Username and password will be asked next.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -m pip install psycopg2-binary -q
  py setup_postgres.py
) else (
  python -m pip install psycopg2-binary -q
  python setup_postgres.py
)
echo.
echo Vercel → Environment Variables:
echo   DATABASE_URL = postgresql://USER:PASSWORD@43.205.3.136:5432/fss_invoice?sslmode=prefer
echo   FLASK_SECRET_KEY = (random long text)
echo   ADMIN_USERNAME / ADMIN_PASSWORD = site login
echo Then Redeploy.
echo.
pause

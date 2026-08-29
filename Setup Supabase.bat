@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
title FSS — Setup Supabase

echo.
echo ============================================================
echo   FSS Invoice Tool — Supabase (24x7, no Azure card)
echo ============================================================
echo.
echo 1. Open https://supabase.com  — Sign in with GitHub
echo 2. New project — wait until it is Ready
echo 3. Project Settings → Database → Connection string → URI
echo    Copy the URI (replace [YOUR-PASSWORD] with DB password)
echo 4. Paste it below
echo.
pause

start https://supabase.com/dashboard

set /p SUPABASE_DB_URL=Supabase URI: 
if "%SUPABASE_DB_URL%"=="" (
    echo URI khali hai.
    pause
    exit /b 1
)

echo.
echo Schema + local data copy...
where py >nul 2>nul
if %errorlevel%==0 (py -m pip install psycopg2-binary -q & py sync_local_to_supabase.py) else (python -m pip install psycopg2-binary -q & python sync_local_to_supabase.py)

echo.
echo ============================================================
echo Vercel → Settings → Environment Variables:
echo   SUPABASE_DB_URL = (same URI)
echo   FLASK_SECRET_KEY = random long text
echo   ADMIN_USERNAME = admin
echo   ADMIN_PASSWORD = your login password
echo Then Deployments → Redeploy
echo Client URL: https://fss-invoice-tool.vercel.app
echo.
start https://vercel.com/sanjaybzpal-codes-projects/fss-invoice-tool/settings/environment-variables
pause

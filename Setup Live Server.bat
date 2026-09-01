@echo off
title FSS — Live on AWS server (no Vercel)
echo.
echo Clients will use the AWS server, not this PC and not Vercel.
echo.
echo 1. AWS Console → EC2 → instance 43.205.3.136 → Connect
echo 2. Paste the commands shown in SERVER_INSTALL.txt
echo 3. AWS → Security Group → Inbound → TCP 80 from 0.0.0.0/0
echo 4. Clients open:  http://43.205.3.136
echo.
start "" notepad "%~dp0SERVER_INSTALL.txt"
pause

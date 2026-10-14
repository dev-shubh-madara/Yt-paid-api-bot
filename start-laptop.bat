@echo off
REM One-click start on Windows: makes a minimal .env if missing, starts the API, prints the public link.
cd /d "%~dp0"
if not exist .env (
  powershell -NoProfile -Command "$t=-join((48..57)+(97..122)|Get-Random -Count 40|%%{[char]$_}); 'ADMIN_TOKEN='+$t | Out-File -Encoding ascii .env; 'MIRROR_FALLBACK=true' | Add-Content -Encoding ascii .env"
  echo Created .env with a random ADMIN_TOKEN. Keep it private.
)
docker compose --profile quick up -d --build
echo Waiting for the public link...
timeout /t 25 /nobreak >nul
docker compose logs cloudflared-quick 2>&1 | findstr /i "trycloudflare.com"
echo.
echo Put the https://....trycloudflare.com link above in the bot as the API URL.
pause

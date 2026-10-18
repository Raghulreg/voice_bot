@echo off
set "NGROK=C:\Users\ADMIN\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
if not exist "%NGROK%" (
  echo ngrok not found. Install with: winget install Ngrok.Ngrok
  exit /b 1
)
echo Starting ngrok tunnel to localhost:8000...
echo Copy the https URL into PUBLIC_URL in .env if it changed.
"%NGROK%" http 8000

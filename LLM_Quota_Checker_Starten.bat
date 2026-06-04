@echo off
setlocal

:: ─────────────────────────────────────────────────
::  LLM Quota Checker – Starter
::  Lege diese .bat-Datei im selben Ordner ab wie
::  llm_quota_checker_gui.py
:: ─────────────────────────────────────────────────

set SCRIPT=%~dp0llm_quota_checker_gui.py

:: ── Python suchen ────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo [FEHLER] Python nicht gefunden.
    echo Bitte installieren: https://www.python.org/downloads/
    pause & exit /b 1
)

:: ── Pakete einzeln prüfen (Importnamen != Paketnamen beachten) ──
echo Pruefe Abhaengigkeiten...

python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo   Installiere requests...
    python -m pip install requests --user --no-warn-script-location -q
)

python -c "import pystray" >nul 2>&1
if errorlevel 1 (
    echo   Installiere pystray...
    python -m pip install pystray --user --no-warn-script-location -q
)

python -c "from PIL import Image" >nul 2>&1
if errorlevel 1 (
    echo   Installiere pillow...
    python -m pip install pillow --user --no-warn-script-location -q
)

:: ── Abschluss-Check ──────────────────────────────
python -c "import requests, pystray; from PIL import Image" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [FEHLER] Ein Paket fehlt. Bitte manuell ausfuehren:
    echo   python -m pip install requests pystray pillow --user
    pause & exit /b 1
)

:: ── Starten (kein Konsolenfenster) ───────────────
echo Alle Pakete OK. Starte LLM Quota Checker...
start "" pythonw "%SCRIPT%"

endlocal
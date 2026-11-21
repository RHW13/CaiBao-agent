@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m streamlit run app.py --server.port 0
) else (
    py -m streamlit run app.py --server.port 0
)

if errorlevel 1 pause

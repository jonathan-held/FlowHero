@echo off
rem Start FlowHero on Windows. The first start creates .venv in this folder
rem and downloads the packages into it; nothing is installed elsewhere.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import flowhero, flowkit, PySide6, pyqtgraph" >nul 2>&1 && goto run
)
set "FH_PY="
for %%V in (3.12 3.11 3.10) do (
  if not defined FH_PY (
    py -%%V -c "import sys" >nul 2>&1 && set "FH_PY=py -%%V"
  )
)
if not defined FH_PY (
  python -c "import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 12)))" >nul 2>&1 && set "FH_PY=python"
)
if not defined FH_PY (
  echo FlowHero needs Python 3.10, 3.11 or 3.12.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then start this file again.
  pause
  exit /b 1
)
echo First start: installing FlowHero's packages into "%cd%\.venv" (needs internet, takes a few minutes).
%FH_PY% -m venv .venv || goto failed
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e . || goto failed
:run
set "PYTHONHOME="
set "PYTHONPATH="
".venv\Scripts\python.exe" -m flowhero %*
if errorlevel 1 pause
exit /b %errorlevel%
:failed
echo Installation failed. Please copy the text above into a bug report.
pause
exit /b 1

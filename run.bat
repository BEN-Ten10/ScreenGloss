@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%ROOT%.conda\pythonw.exe"
if not exist "%PY%" (
  echo [ScreenGloss] Create the Python 3.11 environment first:
  echo   py -3.11 -m venv "%ROOT%.venv"
  echo   "%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
  pause
  exit /b 1
)
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"
set "PIP_CACHE_DIR=%ROOT%.pip-cache"
start "" /D "%ROOT%" "%PY%" -B "%ROOT%app\launcher.py"
endlocal

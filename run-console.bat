@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%ROOT%.conda\python.exe"
if not exist "%PY%" (
  echo [ScreenGloss] Set up the Python 3.11 environment described in README.md.
  pause
  exit /b 1
)
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"
pushd "%ROOT%"
"%PY%" -B "%ROOT%app\launcher.py"
set "APP_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %APP_EXIT%

@echo off
REM ===========================================================
REM  NeutriAI dev launcher
REM
REM  Handles the two things that are easy to forget in a fresh
REM  terminal: being in backend\, and activating the venv.
REM  Runs from anywhere -- %~dp0 is this file's own folder.
REM
REM    dev api       start the API with reload
REM    dev worker    start the background worker
REM    dev smoke     end-to-end smoke test
REM    dev verify    check the Supabase setup
REM    dev url       check SUPABASE_URL and the JWT scheme
REM    dev test      run the pytest suite
REM    dev scan      scan accuracy bench
REM    dev portion   portion estimator bench
REM    dev shell     just drop me in an activated shell
REM ===========================================================
setlocal

cd /d "%~dp0backend"

if not exist ".venv\Scripts\activate.bat" (
  echo.
  echo   No virtualenv found at backend\.venv
  echo   Create it with:
  echo       python -m venv .venv
  echo       .venv\Scripts\activate
  echo       pip install -r requirements.txt
  echo.
  exit /b 1
)

call ".venv\Scripts\activate.bat"

if "%~1"==""        goto :usage
if "%~1"=="api"     goto :api
if "%~1"=="worker"  goto :worker
if "%~1"=="smoke"   goto :smoke
if "%~1"=="verify"  goto :verify
if "%~1"=="url"     goto :url
if "%~1"=="test"    goto :test
if "%~1"=="portion" goto :portion
if "%~1"=="scan"    goto :scan
if "%~1"=="keys"    goto :keys
if "%~1"=="shell"   goto :shell
goto :usage

:api
echo Starting API on http://localhost:8000  (docs at /docs)
python -m uvicorn app.main:app --reload --port 8000
goto :eof

:worker
echo Starting background worker (7 scheduled jobs)
python -m app.workers.scheduler
goto :eof

:smoke
python -m scripts.smoke_test %2 %3
goto :eof

:verify
python -m scripts.verify_supabase
goto :eof

:url
python -m scripts.check_supabase_url
goto :eof

:test
python -m pytest -q
goto :eof

:portion
python -m scripts.portion_lab %2 %3 %4 %5
goto :eof

:scan
python -m scripts.scan_bench %2 %3 %4 %5 %6 %7 %8
goto :eof

:keys
python -m scripts.check_keys
goto :eof

:shell
echo Virtualenv active. You are in backend\.
cmd /k
goto :eof

:usage
echo.
echo   NeutriAI dev launcher
echo.
echo     dev api       start the API      (http://localhost:8000/docs)
echo     dev worker    start the worker
echo     dev smoke     end-to-end smoke test
echo     dev verify    check Supabase setup
echo     dev url       check SUPABASE_URL and JWT scheme
echo     dev test      run the test suite
echo     dev portion   portion estimator bench  (try: dev portion --ladder)
echo     dev keys      check every API key is valid
echo     dev scan      scan accuracy bench      (dev scan photo.jpg --actual "rice=180")
echo     dev shell     activated shell in backend\
echo.
goto :eof

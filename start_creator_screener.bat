@echo off
REM Creator screener launcher. Keep this file ASCII and CRLF only for Windows cmd.
REM Optional: set PORT=8080   set NO_BROWSER=1

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0" 2>nul
if errorlevel 1 (
  echo ERROR cannot cd to script folder.
  pause
  exit /b 1
)

if not exist "tools\creator_screener_server.py" (
  echo ERROR missing tools\creator_screener_server.py
  pause
  exit /b 1
)
if not exist "tools\creator_screener_index.html" (
  echo ERROR missing tools\creator_screener_index.html
  pause
  exit /b 1
)
if not exist "tools\apify_creator_screener.py" (
  echo ERROR missing tools\apify_creator_screener.py
  pause
  exit /b 1
)

set "OPENPORT=5180"
if not "%PORT%"=="" set "OPENPORT=%PORT%"

echo.
echo Creator screener  http://127.0.0.1:%OPENPORT%/
echo Browser opens from Python after bind. Stop: Ctrl+C
echo.

where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  python tools\creator_screener_server.py
  goto AFTER_RUN
)
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  py -3 tools\creator_screener_server.py
  goto AFTER_RUN
)

echo ERROR Python not found. Install Python 3 and add to PATH.
pause
exit /b 1

:AFTER_RUN
echo.
echo Exited code !ERRORLEVEL!
pause
endlocal

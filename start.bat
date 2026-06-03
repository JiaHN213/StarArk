@echo off
title NeuralSpace Server
cd /d "%~dp0"

REM ── Python 路径自动探测 ──────────────────────────────
REM 优先级：环境变量 NEURALSPACE_PYTHON > conda site 环境 > 系统 python
set "PY="
if defined NEURALSPACE_PYTHON (
    if exist "%NEURALSPACE_PYTHON%" set "PY=%NEURALSPACE_PYTHON%"
)
if not defined PY if exist "%USERPROFILE%\.conda\envs\site\python.exe" set "PY=%USERPROFILE%\.conda\envs\site\python.exe"
if not defined PY set "PY=python"

:loop
echo.
echo   =========================================
echo     NeuralSpace - AI Personal Hub
echo     %date% %time%
echo   =========================================
echo     Python: %PY%
echo.
echo   Starting server...
echo.

"%PY%" app.py

echo.
echo   =========================================
echo     Server stopped at %time%
echo     Auto-restarting in 5 seconds...
echo     Press Ctrl+C to stop permanently
echo   =========================================

timeout /t 5 /nobreak >nul
goto loop

@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   [错误] 没找到项目自带的 Python 环境：
    echo          %PY%
    echo.
    echo   若缺失，可在项目根目录执行：
    echo       python -m venv venv
    echo       venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

title AI语音摘要

echo.
echo     AI语音摘要  正在启动...
echo     工作目录: %cd%
echo.
echo.

"%PY%" "%~dp0run.py"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   --------------------------------------------
    echo   [启动失败] 返回码 %RC%
    echo   详细报错已写入: %~dp0startup.log
    echo   --------------------------------------------
    echo.
    pause
)

endlocal

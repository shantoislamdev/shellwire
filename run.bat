@echo off
setlocal

set DIR=%~dp0
set VENV_DIR=%DIR%venv

if not exist "%VENV_DIR%" (
    echo Creating virtual environment in %VENV_DIR%...
    python -m venv "%VENV_DIR%"
    "%VENV_DIR%\Scripts\pip.exe" install -e ".[dev]"
)

"%VENV_DIR%\Scripts\python.exe" -m shellwire %*

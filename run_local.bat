@echo off
setlocal

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo.
    echo ============================================================
    echo  ERROR: no se encontro el entorno virtual en "venv".
    echo  Ejecuta primero la instalacion (crear el venv e instalar
    echo  requirements.txt) antes de usar run_local.bat.
    echo ============================================================
    echo.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo.
    echo ERROR: no se pudo activar el entorno virtual.
    echo.
    pause
    exit /b 1
)

python app_desktop.py
if errorlevel 1 (
    echo.
    echo La aplicacion termino con un error. Revisa el mensaje de arriba.
    echo.
    pause
)

endlocal

@echo off
REM build_exe.bat — compila el .exe de escritorio con PyInstaller.
REM Correr en Windows, DENTRO de la carpeta del proyecto, con el entorno
REM virtual ya creado (ver EMPAQUETADO.md si todavia no existe venv\).
REM PyInstaller no hace cross-compile: este script solo produce un build
REM valido para el sistema operativo en el que se ejecuta.

cd /d "%~dp0"

call venv\Scripts\activate.bat
if errorlevel 1 (
    echo No se pudo activar el entorno virtual ^(venv\Scripts\activate.bat^).
    echo Crea el entorno primero: python -m venv venv
    pause
    exit /b 1
)

echo Instalando dependencias de produccion + empaquetado...
pip install -r requirements.txt -r requirements-dev.txt
if errorlevel 1 (
    echo Fallo la instalacion de dependencias.
    pause
    exit /b 1
)

echo.
echo Compilando con PyInstaller ^(modo carpeta, --onedir^)...
pyinstaller app.spec --noconfirm --clean
if errorlevel 1 (
    echo El build de PyInstaller fallo. Revisa el error de arriba.
    pause
    exit /b 1
)

echo.
echo Build listo en: dist\AuditoriaAylupita\AuditoriaAylupita.exe
echo.
echo IMPORTANTE — falta un paso manual antes de entregar la carpeta:
echo copia tu db.sqlite3 real (con el catalogo y los usuarios ya creados)
echo dentro de dist\AuditoriaAylupita\, junto al .exe. Sin ese paso, el
echo .exe arranca con una base de datos vacia (sin productos ni usuarios).
echo Ver EMPAQUETADO.md para la checklist completa de verificacion.
echo.
pause

@echo off
REM build_exe.bat — compila el .exe de escritorio con PyInstaller.
REM Correr en Windows, DENTRO de la carpeta del proyecto, con el entorno
REM virtual ya creado (ver EMPAQUETADO.md si todavia no existe venv\).
REM PyInstaller no hace cross-compile: este script solo produce un build
REM valido para el sistema operativo en el que se ejecuta.
REM
REM OJO (prompt 33): "pyinstaller --noconfirm" BORRA la carpeta de salida
REM entera antes de reconstruirla — comprobado, imprime "Removing dir
REM ...\dist\AuditoriaAylupita". Eso se lleva por delante el .env y los
REM datos locales que estuvieran ahi. Sin el respaldo/restauracion de
REM abajo, cada rebuild dejaba la app: (a) sin configuracion de nube, o
REM sea conectandose a una base vacia, y (b) sin el cache de credenciales,
REM o sea sin poder iniciar sesion sin internet. Justo los dos sintomas
REM que costaron dias de diagnostico.

cd /d "%~dp0"

set "DESTINO=dist\AuditoriaAylupita"
set "RESPALDO=_respaldo_datos_locales"

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

REM --- Respaldo de lo que el rebuild borraria ---
if exist "%DESTINO%" (
    echo.
    echo Respaldando configuracion y datos locales antes de reconstruir...
    if exist "%RESPALDO%" rmdir /S /Q "%RESPALDO%"
    mkdir "%RESPALDO%"
    if exist "%DESTINO%\.env"                  copy /Y "%DESTINO%\.env" "%RESPALDO%\.env" >nul
    if exist "%DESTINO%\offline_local.sqlite3" copy /Y "%DESTINO%\offline_local.sqlite3" "%RESPALDO%\offline_local.sqlite3" >nul
    if exist "%DESTINO%\sesiones"              xcopy /E /I /Q /Y "%DESTINO%\sesiones" "%RESPALDO%\sesiones" >nul
    if exist "%DESTINO%\backups_completos"     xcopy /E /I /Q /Y "%DESTINO%\backups_completos" "%RESPALDO%\backups_completos" >nul
)

echo.
echo Compilando con PyInstaller ^(modo carpeta, --onedir^)...
pyinstaller app.spec --noconfirm --clean
if errorlevel 1 (
    echo El build de PyInstaller fallo. Revisa el error de arriba.
    echo Tu configuracion y datos locales quedaron a salvo en: %RESPALDO%\
    pause
    exit /b 1
)

REM --- Restauracion ---
if exist "%RESPALDO%" (
    echo Restaurando configuracion y datos locales...
    if exist "%RESPALDO%\.env"                  copy /Y "%RESPALDO%\.env" "%DESTINO%\.env" >nul
    if exist "%RESPALDO%\offline_local.sqlite3" copy /Y "%RESPALDO%\offline_local.sqlite3" "%DESTINO%\offline_local.sqlite3" >nul
    if exist "%RESPALDO%\sesiones"              xcopy /E /I /Q /Y "%RESPALDO%\sesiones" "%DESTINO%\sesiones" >nul
    if exist "%RESPALDO%\backups_completos"     xcopy /E /I /Q /Y "%RESPALDO%\backups_completos" "%DESTINO%\backups_completos" >nul
    rmdir /S /Q "%RESPALDO%"
)

echo.
echo Build listo en: %DESTINO%\AuditoriaAylupita.exe
echo.

if exist "%DESTINO%\.env" (
    echo Configuracion: se restauro el .env que ya estaba junto al .exe.
) else (
    echo IMPORTANTE — falta un paso manual antes de usar o entregar la carpeta:
    echo copia tu archivo .env real ^(el que tiene DATABASE_URL apuntando a
    echo Neon^) dentro de %DESTINO%\, junto al .exe. Ver .env.example.
    echo.
    echo Sin ese archivo la app NO se conecta a la nube: arranca igual, pero
    echo todo lo que se registre se queda en este equipo y nunca sincroniza.
    echo NOTA: la instruccion vieja de copiar db.sqlite3 YA NO APLICA.
)

echo.
echo Recuerda: si el cache de credenciales se perdio, cada usuario tiene que
echo iniciar sesion UNA VEZ CON INTERNET antes de poder entrar sin conexion.
echo Ver checklist_pruebas_manuales_windows.md ^(Punto 0-bis^).
echo.
pause

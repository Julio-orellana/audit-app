# app.spec
"""
Spec de PyInstaller para empaquetar la app de escritorio en modo carpeta
(--onedir): más confiable que --onefile con Django (no tiene que
autoextraerse en un directorio temporal en cada arranque) y de inicio más
rápido. Ver EMPAQUETADO.md para instrucciones de build y verificación.

Se compila SOLO en Windows (o en el sistema operativo destino — PyInstaller
no hace cross-compile). Uso:

    pyinstaller app.spec --noconfirm --clean

Genera dist/AuditoriaAylupita/AuditoriaAylupita.exe.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# --- Datos: templates, estáticos y archivos de paquetes que Django y las
# librerías de terceros leen del disco en tiempo de ejecución (no son
# bytecode importable — PyInstaller no los detecta solo). ------------------
datas = [
    ("templates", "templates"),
    ("static", "static"),
    ("inventario/templates", "inventario/templates"),
]
for paquete in ("crispy_forms", "crispy_bootstrap5", "reportlab", "django.contrib.admin", "django.contrib.auth"):
    datas += collect_data_files(paquete)

# --- Imports ocultos: Django resuelve INSTALLED_APPS, migraciones y
# management commands por nombre de módulo en tiempo de ejecución (strings
# en settings.py / metadata de la BD), no por "import X" estático — el
# análisis de PyInstaller no los ve solo y hay que declararlos a mano.
hiddenimports = []
for paquete in (
    "inventario",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crispy_forms",
    "crispy_bootstrap5",
    "webview",
):
    hiddenimports += collect_submodules(paquete)

a = Analysis(
    ["app_desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

# El hook incluido en PyInstaller para Django (hook-django.py) detecta
# automáticamente cualquier *.db / db.* junto a manage.py y lo empaqueta
# como dato de solo lectura dentro del bundle (_internal/) — quedaría ahí
# una FOTO CONGELADA de la base de datos real tal como estaba en el
# momento de compilar (con las contraseñas hasheadas de los usuarios
# reales incluidas). Nunca se usa en tiempo de ejecución porque
# settings.py siempre apunta a BASE_DIR_ESCRIBIBLE (junto al .exe real,
# no al bundle), pero de todos modos no debe quedar embebida en el
# ejecutable: la base de datos real se copia aparte, a mano, junto al
# .exe (ver EMPAQUETADO.md) — nunca horneada de solo lectura dentro del
# propio programa.
a.datas = [d for d in a.datas if not (d[0] == "db.sqlite3" or d[0].startswith("db."))]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AuditoriaAylupita",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # sin ventana de terminal — la app real es la ventana de pywebview
    icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="AuditoriaAylupita",
)

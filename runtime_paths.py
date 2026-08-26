# runtime_paths.py
"""
Rutas base del proyecto, sensibles a si el programa corre desde código
fuente (`python app_desktop.py` / `python manage.py ...`) o empaquetado
con PyInstaller en modo carpeta (`--onedir`).

Cuando PyInstaller empaqueta la app, `sys.frozen` existe. El código en sí
(templates, estáticos, apps de Django) vive extraído en una carpeta de
solo lectura — en PyInstaller moderno, normalmente `dist/<app>/_internal`,
localizada vía `sys._MEIPASS`. Nunca se debe escribir ahí: en una
instalación real esa carpeta puede vivir en una ubicación protegida
(ej. "Archivos de programa"), y conceptualmente es contenido empaquetado,
no datos del usuario.

La base de datos, los respaldos y cualquier archivo que deba sobrevivir
al cierre del programa tienen que vivir junto al .exe real
(`sys.executable`), no dentro del bundle — esa es la carpeta que el
usuario controla y que persiste entre ejecuciones.

Cuando NO está congelado (desarrollo normal), ambas rutas son la misma:
la carpeta donde vive este archivo.
"""
import sys
from pathlib import Path


def esta_empaquetado():
    """True si el proceso corre desde un build de PyInstaller."""
    return getattr(sys, "frozen", False)


def carpeta_codigo():
    """Carpeta de solo lectura con el código, templates y estáticos."""
    if esta_empaquetado():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def carpeta_escribible():
    """Carpeta donde SÍ se puede crear/modificar archivos (BD, backups)."""
    if esta_empaquetado():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

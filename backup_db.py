# backup_db.py
"""
Respaldo local de la base de datos, totalmente independiente del sistema
POS principal (mismo patrón que usa ese sistema: copiar el .sqlite3 con
fecha y hora, pero aquí sin ninguna conexión entre los dos proyectos).

Se ejecuta cada vez que se abre la app de escritorio (ver app_desktop.py),
antes de levantar el servidor.
"""
import shutil
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "db.sqlite3"
BACKUPS_DIR = BASE_DIR / "backups"


def hacer_backup():
    """
    Copia db.sqlite3 a backups/db_{YYYYmmdd}_{HHMMSS}.sqlite3.
    Devuelve la ruta del backup creado, o None si todavía no existe la BD
    (primera ejecución de la app, antes de correr las migraciones).
    """
    if not DB_PATH.exists():
        return None

    BACKUPS_DIR.mkdir(exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = BACKUPS_DIR / f"db_{marca}.sqlite3"
    shutil.copy2(DB_PATH, destino)
    return destino


if __name__ == "__main__":
    ruta = hacer_backup()
    if ruta:
        print(f"Backup creado: {ruta}")
    else:
        print("No hay base de datos todavía (db.sqlite3 no existe); no se creó backup.")

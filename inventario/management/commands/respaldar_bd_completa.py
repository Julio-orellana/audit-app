# inventario/management/commands/respaldar_bd_completa.py
"""
Respaldo local completo de toda la base de datos en la nube (prompt 23)
— distinto de backups/ (la copia de db.sqlite3 de backup_db.py, hoy solo
relevante corriendo contra SQLite local, y de la futura cola de
sincronización offline del prompt 19). Este vive en backups_completos/,
una carpeta separada a propósito para no mezclar los dos conceptos: si
algún día se pierde el acceso a Neon de forma permanente, aquí hay una
copia completa y reciente de todo el sistema para reconstruirlo desde
cero.

Usa `dumpdata` (ya viene con Django, no agrega ninguna dependencia nueva
al .exe empaquetado) — no hace falta pg_dump externo.

Como máximo un respaldo por día (se salta la operación si ya existe uno
de hoy — no tiene sentido repetirlo cada vez que se abre la app el mismo
día) y borra automáticamente los de más de 30 días, para que la carpeta
no crezca indefinidamente.

Se dispara automáticamente después de un login exitoso de admin/auditor
(nunca vendedor) — ver inventario/respaldos.py — pero también se puede
correr a mano: `python manage.py respaldar_bd_completa`.
"""
import datetime
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand

from runtime_paths import carpeta_escribible

NOMBRE_CARPETA = "backups_completos"
PREFIJO_ARCHIVO = "respaldo_completo_"
DIAS_RETENCION = 30

# Se excluyen del dump porque Django ya las recrea solas al correr
# migrate en una base nueva (contenidos automáticos, no datos reales del
# negocio) — incluirlas complicaría innecesariamente una restauración con
# loaddata sobre una base recién migrada (podrían chocar con lo que
# migrate ya insertó). sessions tampoco vale la pena respaldarla: son
# sesiones de navegador activas, no información que se pierda de verdad.
APPS_Y_MODELOS_EXCLUIDOS = ["contenttypes", "auth.permission", "sessions.session", "admin.logentry"]


class Command(BaseCommand):
    help = "Genera un respaldo completo en JSON de toda la base de datos (máx. 1 por día) y borra los de más de 30 días."

    def handle(self, *args, **options):
        carpeta = carpeta_escribible() / NOMBRE_CARPETA
        carpeta.mkdir(exist_ok=True)

        hoy = datetime.date.today()
        prefijo_hoy = f"{PREFIJO_ARCHIVO}{hoy.isoformat()}_"
        ya_existe_hoy = any(carpeta.glob(f"{prefijo_hoy}*.json"))

        if ya_existe_hoy:
            self.stdout.write(f"Ya existe un respaldo completo de hoy ({hoy}) — no se genera otro.")
        else:
            nombre_archivo = f"{PREFIJO_ARCHIVO}{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
            ruta = carpeta / nombre_archivo
            with open(ruta, "w", encoding="utf-8") as archivo:
                call_command(
                    "dumpdata",
                    exclude=APPS_Y_MODELOS_EXCLUIDOS,
                    natural_foreign=True,
                    natural_primary=True,
                    indent=2,
                    stdout=archivo,
                )
            self.stdout.write(self.style.SUCCESS(f"Respaldo completo generado: {ruta.name}"))

        eliminados = self._limpiar_respaldos_viejos(carpeta)
        if eliminados:
            self.stdout.write(self.style.SUCCESS(f"Eliminados {eliminados} respaldo(s) de más de {DIAS_RETENCION} días."))

    def _limpiar_respaldos_viejos(self, carpeta):
        limite = time.time() - DIAS_RETENCION * 86400
        eliminados = 0
        for archivo in carpeta.glob(f"{PREFIJO_ARCHIVO}*.json"):
            if archivo.stat().st_mtime < limite:
                archivo.unlink()
                eliminados += 1
        return eliminados

# inventario/respaldos.py
"""
Dispara el respaldo completo diario de la base de datos (prompt 23,
comando respaldar_bd_completa) justo después de un login exitoso de
admin o auditor — nunca de vendedor, restricción intencional del
requerimiento original.

Por qué después del login y no al abrir la app (app_desktop.py, donde ya
corren el auto-migrate y el backup de db.sqlite3): a diferencia de esos
dos, que no necesitan saber quién va a usar la app, este respaldo SÍ
depende del rol de quien inició sesión — y al arrancar la app todavía no
ha iniciado sesión nadie. django.contrib.auth.signals.user_logged_in es
el punto exacto donde Django ya sabe quién entró, para cualquier login
(no solo el LoginView por defecto), sin tener que tocar la vista de login.
"""
import logging
import sys

from django.contrib.auth.signals import user_logged_in
from django.core.management import call_command

logger = logging.getLogger(__name__)

ROLES_QUE_DISPARAN_RESPALDO = ("admin", "auditor")


def _corriendo_pruebas():
    """
    True si el proceso actual es "manage.py test". Un login DENTRO de una
    prueba (ej. HistorialCompletoTests, que hace login como un usuario del
    grupo auditor) también dispara la señal user_logged_in — sin este
    guard, correr las pruebas terminaría escribiendo un volcado de la
    base de datos DE PRUEBA a backups_completos/, en el sistema de
    archivos real (el aislamiento de Django en las pruebas es solo de la
    base de datos, nunca del disco) — y si las pruebas corren antes que
    cualquier login real en el día, ESE sería el "respaldo del día",
    dejando fuera los datos reales.
    """
    return len(sys.argv) > 1 and sys.argv[1] == "test"


def _respaldar_si_corresponde(sender, request, user, **kwargs):
    if _corriendo_pruebas():
        return
    if not user.groups.filter(name__in=ROLES_QUE_DISPARAN_RESPALDO).exists():
        return
    try:
        call_command("respaldar_bd_completa")
    except Exception:
        # Un respaldo fallido (ej. disco lleno, la nube inalcanzable en
        # este instante) no debe impedir que el usuario pueda entrar y
        # seguir usando la app — se registra en el log y no se propaga.
        logger.exception("Falló el respaldo completo automático tras el login.")


def conectar_señales():
    user_logged_in.connect(_respaldar_si_corresponde, dispatch_uid="respaldo_completo_tras_login")

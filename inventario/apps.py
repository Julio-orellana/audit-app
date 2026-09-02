from django.apps import AppConfig


class InventarioConfig(AppConfig):
    name = 'inventario'

    def ready(self):
        from . import respaldos

        respaldos.conectar_señales()

        # Discrepancias de inventario (prompt 34): registrar la de cada
        # conteo nuevo, y marcar las que quedan afectadas cuando llega un
        # movimiento con fecha anterior. Importar el módulo basta — los
        # @receiver se conectan solos.
        from . import signals  # noqa: F401

        # Hilo de sincronización offline (prompt 19b, punto 2): antes
        # SOLO arrancaba desde app_desktop.py, así que levantar el
        # proyecto con `manage.py runserver` — justo como se prueba en
        # desarrollo — no sincronizaba absolutamente nada. Arrancarlo
        # aquí lo hace válido para cualquier forma de levantar la app.
        # La función se encarga sola de no arrancarlo donde no
        # corresponde (pruebas, migraciones, el proceso vigilante del
        # autoreloader).
        from .offline import iniciar_hilo_sincronizacion_si_corresponde

        iniciar_hilo_sincronizacion_si_corresponde()

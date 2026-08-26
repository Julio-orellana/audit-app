from django.apps import AppConfig


class InventarioConfig(AppConfig):
    name = 'inventario'

    def ready(self):
        from . import respaldos

        respaldos.conectar_señales()

# inventario/management/commands/limpiar_datos_prueba.py
"""
Elimina todos los movimientos de prueba (LoteCompra, MovimientoSalida,
ConteoFisico) para dejar la base de datos lista para que el auditor
empiece a usarla en real. Conserva intactos el catálogo (Categoria,
Producto) y los usuarios.

Confirmación obligatoria (prompt 32): este comando hace exactamente el
mismo borrado — sobre los mismos tres modelos — que el script que causó
el incidente del prompt 30 (una base de pruebas mal aislada terminó
apuntando a producción real, sin ninguna pregunta de por medio antes de
borrar). A diferencia de aquel script, ESTE comando SÍ está pensado para
correr contra producción — es su propósito real, una sola vez, al pasar
de datos de prueba a uso real — así que la corrección no es bloquearlo
fuera de producción, sino nunca dejarlo correr sin mostrar antes,
explícitamente, contra qué base de datos está a punto de escribir y sin
una confirmación real del operador. Ver seguridad_entorno_pruebas.py.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventario.models import ConteoFisico, LoteCompra, MovimientoSalida, Producto
from seguridad_entorno_pruebas import EntornoNoSeguroError, confirmar_operacion_riesgosa


class Command(BaseCommand):
    help = (
        "Elimina LoteCompra, MovimientoSalida y ConteoFisico (datos de prueba), "
        "conservando Categoria y Producto intactos. No borra usuarios. Pide "
        "confirmación explícita salvo que se pase --sin-confirmar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sin-confirmar", action="store_true", dest="sin_confirmar",
            help="Omite la confirmación interactiva (uso en automatización ya controlada).",
        )

    def handle(self, *args, **options):
        try:
            confirmar_operacion_riesgosa(
                "borrar TODO LoteCompra, MovimientoSalida y ConteoFisico",
                forzar=options["sin_confirmar"],
            )
        except EntornoNoSeguroError as error:
            self.stderr.write(self.style.ERROR(str(error)))
            return

        with transaction.atomic():
            # ConteoFisico primero: aunque ajuste_generado usa on_delete=SET_NULL
            # (no hay error de integridad en ningún orden), es más claro borrar
            # primero lo que solo "observa" el stock y después lo que lo mueve.
            n_conteos = ConteoFisico.objects.all().delete()[0]
            n_salidas = MovimientoSalida.objects.all().delete()[0]
            n_lotes = LoteCompra.objects.all().delete()[0]

        self.stdout.write(self.style.SUCCESS(
            f"Eliminados: {n_lotes} LoteCompra, {n_salidas} MovimientoSalida, "
            f"{n_conteos} ConteoFisico."
        ))

        # Verificación: sin compras ni salidas, stock_teorico() debe dar 0 para
        # TODOS los productos de forma natural (es un cálculo, no un campo
        # aparte) — si algo no da 0, algo quedó sin borrar.
        productos = list(Producto.objects.all())
        con_stock_residual = [p for p in productos if p.stock_teorico() != 0]

        if con_stock_residual:
            detalle = ", ".join(f"{p.nombre} ({p.stock_teorico()})" for p in con_stock_residual)
            self.stderr.write(self.style.ERROR(
                f"ADVERTENCIA: {len(con_stock_residual)} producto(s) NO quedaron en stock 0: {detalle}"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Verificado: los {len(productos)} productos del catálogo quedaron con "
                f"stock_teorico() = 0."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Catálogo conservado sin cambios: {Producto.objects.count()} productos en "
            f"{Producto.objects.values('categoria').distinct().count()} categoría(s) con productos."
        ))

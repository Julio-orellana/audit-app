# inventario/signals.py
"""
Señales que mantienen las discrepancias de inventario al día (prompt 34).

Van en señales y no en las vistas a propósito: un conteo puede nacer por
tres caminos —el formulario con conexión, la cola offline al sincronizar,
o el admin de Django— y los tres tienen que registrar su discrepancia. Si
esto viviera en `ConteoFisicoCreateView.form_valid()`, el conteo que
llega por sincronización —que es exactamente el caso del escenario 1—
entraría sin discrepancia y el problema seguiría vivo justo donde más
duele.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger("inventario.discrepancias")


def _es_escritura_real(using, raw):
    """
    Solo interesa lo que se escribe en la base de la nube.

    - `raw=True` es una carga de fixture: los objetos relacionados pueden
      no existir todavía y calcular stock ahí revienta.
    - Los alias locales ("local_disco") guardan la cola y el catálogo
      cacheado; las discrepancias viven solo en "default" (ver
      db_router.MODELOS_PERMITIDOS_EN_LOCALES).
    """
    return not raw and using == "default"


@receiver(post_save, sender="inventario.ConteoFisico", dispatch_uid="discrepancia_de_conteo")
def crear_discrepancia_de_conteo(sender, instance, created, raw=False, using=None, **kwargs):
    if not created or not _es_escritura_real(using, raw):
        return
    from .discrepancias import registrar_discrepancia

    try:
        registrar_discrepancia(instance)
    except Exception:
        # Un fallo aquí no puede tumbar el guardado del conteo: perder el
        # conteo es peor que perder su discrepancia, y el conteo es el
        # dato que la persona tomó a mano y no puede volver a tomar.
        logger.exception(
            "No se pudo registrar la discrepancia del conteo #%s. El conteo SÍ quedó guardado.",
            instance.pk,
        )


@receiver(post_save, sender="inventario.MovimientoSalida", dispatch_uid="revisar_discrepancias_salida")
@receiver(post_save, sender="inventario.LoteCompra", dispatch_uid="revisar_discrepancias_compra")
def revisar_discrepancias_afectadas(sender, instance, created, raw=False, using=None, **kwargs):
    """
    Un movimiento con fecha/instante ANTERIOR a una discrepancia pendiente
    cambia el pasado de esa discrepancia (prompt 34, decisión 2).

    Pasa de verdad: alguien registra hoy una compra con fecha de antier, o
    un movimiento registrado sin conexión sincroniza tarde. El
    `teorico_al_conteo` congelado de las discrepancias posteriores queda
    viejo. No se corrige solo —eso es lo que se está eliminando— sino que
    se recalcula aparte y se marca para que una persona lo mire.
    """
    if not created or not _es_escritura_real(using, raw):
        return
    if getattr(instance, "_de_resolucion", False):
        # Nace de resolver una discrepancia: resolver_discrepancia() ya
        # avisa a las posteriores, y con un motivo que dice cuál fue.
        return
    from .discrepancias import marcar_afectadas_por

    producto = instance.producto
    # Una salida de un producto derivado consume stock del base, así que
    # afecta a las discrepancias del BASE, que es donde vive el inventario
    # real (ver Producto.stock_teorico).
    producto_id = producto.producto_base_id or producto.pk

    try:
        afectadas = marcar_afectadas_por(
            producto_id, instance.fecha, instance.ocurrido_en,
            motivo=(
                f"Se registró un movimiento con fecha {instance.fecha:%d/%m/%Y} "
                f"anterior o simultáneo a este conteo, después de que la discrepancia "
                f"quedara calculada."
            ),
        )
    except Exception:
        logger.exception(
            "No se pudieron revisar las discrepancias afectadas por %s #%s.",
            sender.__name__, instance.pk,
        )
        return

    # Conflicto MENOR (prompt 34 punto 1, completado en el 37): el
    # movimiento llegó fuera de orden pero no cambió ninguna diferencia
    # pendiente. Se deja constancia y no se bloquea nada. Si SÍ hubiera
    # cambiado alguna, `afectadas` sería > 0 y arriba ya quedó marcada
    # para revisión humana — entonces esta nota no se escribe.
    try:
        from .discrepancias import registrar_nota_conflicto_menor

        registrar_nota_conflicto_menor(instance, afectadas)
    except Exception:
        # Una nota informativa jamás puede tumbar el guardado del
        # movimiento que la originó.
        logger.exception(
            "No se pudo registrar la nota de conflicto menor de %s #%s.",
            sender.__name__, instance.pk,
        )

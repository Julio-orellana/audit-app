# inventario/discrepancias.py
"""
Discrepancias de inventario: detectarlas, mostrarlas con contexto y
resolverlas SIEMPRE con una persona de por medio (prompt 34).

El diseño anterior tenía dos problemas de fondo, no de cálculo:

1. **No había ancla temporal.** `fecha` es un DateField, así que dentro
   de un mismo día no existe orden. Un conteo de las 15:03 se comparaba
   contra el stock teórico "de su fecha", que ya incluía una venta de las
   15:05 — la venta se contaba dos veces, una en el teórico y otra en la
   realidad del piso, y un faltante de 5 aparecía como un sobrante de 1.

2. **La discrepancia no existía como registro.** Era una resta en vivo, y
   la alerta del tablero era literalmente `if diferencia != 0`. Cuando un
   movimiento posterior hacía cuadrar los números, la alerta desaparecía
   sola: nadie la había resuelto, simplemente dejaba de calcularse. El
   faltante real seguía ahí, ahora sin ninguna señal.

Este módulo invierte las dos cosas: la diferencia se calcula contra el
instante exacto del conteo y se CONGELA en un DiscrepanciaInventario, que
solo cambia de estado porque alguien lo cambió.
"""
import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("inventario.discrepancias")


def registrar_discrepancia(conteo):
    """
    Crea la discrepancia de un conteo recién guardado, si hay diferencia.

    Se llama desde una señal post_save (ver signals.py) para cubrir por
    igual el conteo registrado con conexión y el que llega por la cola
    offline, sin depender de que cada camino se acuerde de invocarla.

    Nunca toca el stock. Devuelve la discrepancia creada, o None si el
    conteo cuadró exacto.
    """
    from .models import DiscrepanciaInventario

    teorico = conteo.producto.stock_teorico(
        hasta_fecha=conteo.fecha, hasta_instante=conteo.ocurrido_en
    )
    diferencia = conteo.cantidad_contada - teorico
    if diferencia == 0:
        return None

    discrepancia, creada = DiscrepanciaInventario.objects.get_or_create(
        conteo=conteo,
        defaults={
            "producto_id": conteo.producto_id,
            "contado": conteo.cantidad_contada,
            "teorico_al_conteo": teorico,
            "diferencia": diferencia,
        },
    )
    if creada:
        logger.info(
            "Discrepancia registrada: %s el %s — contado %d contra %d teórico al momento "
            "del conteo (%s) = %+d. Queda PENDIENTE de revisión humana; no se movió stock.",
            conteo.producto, conteo.fecha, conteo.cantidad_contada, teorico,
            conteo.ocurrido_en.isoformat(), diferencia,
        )
    return discrepancia


def marcar_afectadas_por(producto_id, fecha, ocurrido_en, motivo, excluir_id=None):
    """
    Un movimiento entró en el PASADO de discrepancias que ya estaban
    congeladas: recalcula y marca, sin aplicar nada (prompt 34,
    decisiones 1 y 2).

    Pasa en dos situaciones y el trato es el mismo:

    - se resuelve una discrepancia más antigua, y su ajuste queda con la
      fecha e instante de su conteo, o sea antes de otras pendientes;
    - alguien registra un movimiento con fecha hacia atrás.

    En los dos casos el `teorico_al_conteo` congelado de las posteriores
    quedó viejo. Recalcular en silencio sería repetir el error original
    (que un número cambie solo); aplicarlo sería peor. Se guarda el valor
    nuevo APARTE y se marca `requiere_revision`, para que la pantalla
    muestre el número viejo y el nuevo y decida una persona.
    """
    from .models import DiscrepanciaInventario

    posteriores = (
        DiscrepanciaInventario.objects
        .filter(producto_id=producto_id, estado=DiscrepanciaInventario.PENDIENTE)
        .select_related("conteo", "producto")
    )
    if excluir_id is not None:
        posteriores = posteriores.exclude(pk=excluir_id)

    marcadas = 0
    for discrepancia in posteriores:
        conteo = discrepancia.conteo
        # Solo afecta a las que vienen DESPUÉS: el par (fecha, instante)
        # es la clave de orden de todo el sistema.
        if (conteo.fecha, conteo.ocurrido_en) < (fecha, ocurrido_en):
            continue

        teorico_nuevo = conteo.producto.stock_teorico(
            hasta_fecha=conteo.fecha, hasta_instante=conteo.ocurrido_en
        )
        if teorico_nuevo == discrepancia.teorico_al_conteo:
            continue

        discrepancia.teorico_recalculado = teorico_nuevo
        discrepancia.diferencia_recalculada = conteo.cantidad_contada - teorico_nuevo
        discrepancia.requiere_revision = True
        discrepancia.motivo_revision = motivo
        discrepancia.save(update_fields=[
            "teorico_recalculado", "diferencia_recalculada",
            "requiere_revision", "motivo_revision",
        ])
        marcadas += 1
        logger.info(
            "Discrepancia #%d marcada para revisar de nuevo: su teórico pasó de %d a %d "
            "(diferencia %+d -> %+d). Motivo: %s",
            discrepancia.pk, discrepancia.teorico_al_conteo, teorico_nuevo,
            discrepancia.diferencia, discrepancia.diferencia_recalculada, motivo,
        )
    return marcadas


# Un movimiento que llega con este desfase o más entre su instante REAL
# (ocurrido_en) y el momento en que la base lo insertó (creado_en) no se
# registró en línea: venía de la cola offline y sincronizó tarde. Ese es
# el caso en que un movimiento se INSERTA EN EL PASADO de cosas que ya
# estaban en el sistema — el conflicto de orden del prompt 34.
#
# 60 segundos para no confundirlo con la latencia normal de una escritura
# en línea (medida contra Neon: ~120 ms por consulta).
SEGUNDOS_DESFASE_PARA_NOTA = 60


def registrar_nota_conflicto_menor(instance, discrepancias_afectadas):
    """
    Deja constancia de un conflicto de orden que el sistema detectó y
    descartó SOLO porque no cambió ningún resultado (prompt 34 punto 1,
    completado en el 37).

    Qué es un conflicto MENOR aquí, con precisión — las tres condiciones
    se exigen juntas:

    1. El movimiento llegó fuera de orden: su instante real es anterior
       al momento en que la base lo recibió, o sea que venía de la cola
       offline y sincronizó tarde.
    2. Cae en el pasado de al menos un conteo YA CERRADO (resuelto o
       descartado) del mismo producto, y de verdad le mueve el teórico.
    3. No afectó a ninguna discrepancia PENDIENTE.

    La condición 2 es la que hace que esto valga la pena escribirlo. Un
    conteo cerrado no se reabre: su ajuste ya está aplicado y su
    resultado financiero ya está contabilizado — esa fue la decisión del
    prompt 34, y no se cambia aquí. Pero el número con el que se cerró se
    calculó sin este movimiento. Nadie tiene que hacer nada, y aun así el
    historial no puede quedarse mudo al respecto. Eso es exactamente una
    nota informativa: constancia sin bloqueo.

    El caso MAYOR —el movimiento tardío cambia el teórico congelado de
    una discrepancia PENDIENTE— ya lo cubre marcar_afectadas_por()
    poniendo requiere_revision, y entonces aquí no se escribe nada: sería
    ruido al lado de algo que sí exige a una persona.

    Y el caso corriente —una venta offline que sincroniza tarde y no cae
    en el pasado de ningún conteo— no genera nada. Esto importa: sin la
    condición 2, CADA movimiento sincronizado desde la cola dejaría una
    nota, y el Historial quedaría inservible de tanto ruido (se comprobó:
    duplicaba el número de filas de la pantalla).

    NOTA SOBRE EL DISEÑO ORIGINAL (importante para quien lea esto
    después): el prompt 34 dejó preparada una rama para el conflicto
    menor dentro de marcar_afectadas_por() —"si el teórico recalculado da
    igual, no hagas nada"— y al completar esto se comprobó que esa rama
    es INALCANZABLE: instrumentada, no se ejecutó ni una vez en las 75
    pruebas de la suite, y estructuralmente no puede ejecutarse, porque
    cualquier movimiento que caiga en el pasado de una discrepancia
    pendiente siempre entra en el filtro de stock_teorico() y por lo
    tanto siempre cambia el número. Rellenar aquella rama habría sido
    construir algo que nunca se ejecuta. El conflicto menor se detecta
    donde de verdad ocurre: al sincronizar tarde.
    """
    from .models import CorreccionHistorial

    ocurrido_en = getattr(instance, "ocurrido_en", None)
    creado_en = getattr(instance, "creado_en", None)
    if ocurrido_en is None or creado_en is None:
        return None
    desfase = (creado_en - ocurrido_en).total_seconds()
    if desfase < SEGUNDOS_DESFASE_PARA_NOTA:
        return None            # se registró en línea: no hubo conflicto de orden
    if discrepancias_afectadas:
        return None            # es un conflicto MAYOR: ya hay requiere_revision

    cerradas = _cerradas_alcanzadas_por(instance)
    if not cerradas:
        return None            # sincronizó tarde, pero no alcanzó a ningún conteo cerrado

    minutos = desfase / 60
    nota = CorreccionHistorial.objects.create(
        tipo_registro=type(instance).__name__,
        registro_id=instance.pk,
        accion=CorreccionHistorial.ACCION_NOTA,
        datos_anteriores={},
        datos_nuevos={
            "ocurrido_en": ocurrido_en.isoformat(),
            "creado_en": creado_en.isoformat(),
            "desfase_segundos": round(desfase),
            "producto_id": instance.producto_id,
            # La fecha contable del movimiento, para que la nota se pueda
            # ubicar en el Historial sin volver a consultar su tabla.
            "fecha": instance.fecha.isoformat(),
            "conteos_cerrados_alcanzados": [d.pk for d in cerradas],
        },
        motivo=(
            f"Conflicto de orden menor: este movimiento se registró {_texto_desfase(minutos)} "
            f"antes de llegar al sistema (venía sin conexión), así que quedó insertado en el "
            f"pasado del historial, por detrás de "
            f"{len(cerradas)} conteo{'s' if len(cerradas) > 1 else ''} que ya estaba"
            f"{'n' if len(cerradas) > 1 else ''} cerrado{'s' if len(cerradas) > 1 else ''}. "
            f"Esos conteos no se reabren y su ajuste sigue en pie, así que no hace falta que "
            f"nadie revise nada ni cambia ningún resultado. Queda esta constancia para que el "
            f"orden del historial sea explicable."
        ),
        realizado_por=None,     # lo escribió el sistema, no una persona
    )
    logger.info(
        "Nota automática #%d: %s #%d llegó con %.0f min de desfase, por detrás de "
        "%d conteo(s) ya cerrado(s) %s, y no afectó a ninguna diferencia pendiente. "
        "Se registró la constancia; no se bloqueó nada.",
        nota.pk, type(instance).__name__, instance.pk, minutos,
        len(cerradas), [d.pk for d in cerradas],
    )
    return nota


def _cerradas_alcanzadas_por(instance):
    """
    Discrepancias YA CERRADAS del producto que este movimiento deja
    desactualizadas: las que anclan en el mismo instante o después, y a
    las que este movimiento de verdad les mueve el teórico.

    Solo lee y compara; no marca ni recalcula nada guardado — un conteo
    cerrado no cambia, esa es justamente la premisa.
    """
    from .models import DiscrepanciaInventario

    candidatas = (
        DiscrepanciaInventario.objects
        .filter(producto_id=instance.producto_id)
        .exclude(estado=DiscrepanciaInventario.PENDIENTE)
        .select_related("conteo", "producto")
    )
    alcanzadas = []
    for discrepancia in candidatas:
        conteo = discrepancia.conteo
        if (conteo.fecha, conteo.ocurrido_en) < (instance.fecha, instance.ocurrido_en):
            continue
        teorico_ahora = conteo.producto.stock_teorico(
            hasta_fecha=conteo.fecha, hasta_instante=conteo.ocurrido_en
        )
        if teorico_ahora == discrepancia.teorico_al_conteo:
            continue
        alcanzadas.append(discrepancia)
    return alcanzadas


def _texto_desfase(minutos):
    """'unos 3 minutos' / 'unas 2 horas' — para que la nota se lea sola."""
    if minutos < 60:
        return f"unos {round(minutos)} minutos"
    horas = minutos / 60
    if horas < 24:
        return f"unas {round(horas)} horas"
    return f"unos {round(horas / 24)} días"


@transaction.atomic
def resolver_discrepancia(discrepancia, cantidad_ajuste, usuario, nota=""):
    """
    Aplica el ajuste que una PERSONA confirmó. Es el único camino por el
    que una discrepancia mueve stock.

    `cantidad_ajuste` viene de la pantalla y puede diferir de
    `ajuste_sugerido`: el sistema propone, la persona decide. Con 0 no se
    crea ningún movimiento — es "revisado, no hay nada que ajustar", que
    es distinto de descartarla sin mirar.

    El ajuste se crea con la fecha y el INSTANTE del conteo, no con los de
    ahora: corrige la realidad en el momento en que se detectó. Por eso
    entra en el pasado de otras discrepancias pendientes, y por eso hay
    que marcarlas.
    """
    from .models import DiscrepanciaInventario, MovimientoSalida

    if not discrepancia.esta_pendiente:
        raise ValueError("Esta discrepancia ya fue resuelta.")

    conteo = discrepancia.conteo
    ajuste = None
    if cantidad_ajuste != 0:
        ajuste = MovimientoSalida(
            producto_id=discrepancia.producto_id,
            fecha=conteo.fecha,
            ocurrido_en=conteo.ocurrido_en,
            tipo="ajuste",
            cantidad=cantidad_ajuste,
            motivo=(
                f"Ajuste confirmado por revisión de la discrepancia #{discrepancia.pk} "
                f"(conteo físico #{conteo.pk})"
            ),
            registrado_por_id=getattr(usuario, "pk", None),
        )
        # Marca para la señal: este movimiento nace de resolver una
        # discrepancia, así que el aviso a las posteriores lo da esta
        # función —que sabe cuál fue— y no el mensaje genérico del
        # post_save.
        ajuste._de_resolucion = True
        ajuste.save()
        # Compatibilidad con lo que ya existía: la pantalla del conteo y
        # los reportes miran ConteoFisico.ajuste_generado.
        conteo.ajuste_generado = ajuste
        conteo.save(update_fields=["ajuste_generado"])

    discrepancia.estado = DiscrepanciaInventario.RESUELTA
    discrepancia.cantidad_confirmada = cantidad_ajuste
    discrepancia.ajuste = ajuste
    discrepancia.nota_resolucion = nota
    discrepancia.resuelta_por_id = getattr(usuario, "pk", None)
    discrepancia.resuelta_en = timezone.now()
    # El ajuste que se acaba de crear lleva la fecha e instante del
    # conteo, así que el post_save lo ve como "un movimiento en el pasado
    # de esta discrepancia" y la marca a ella misma antes de llegar aquí.
    # Se limpia: resolverla es precisamente dejar de necesitar revisión.
    discrepancia.requiere_revision = False
    discrepancia.motivo_revision = ""
    discrepancia.teorico_recalculado = None
    discrepancia.diferencia_recalculada = None
    discrepancia.save()

    logger.info(
        "Discrepancia #%d RESUELTA por %s con ajuste %+d (sugerido %+d).",
        discrepancia.pk, getattr(usuario, "username", "?"),
        cantidad_ajuste, discrepancia.ajuste_sugerido,
    )

    if ajuste is not None:
        # Se marca desde aquí y no desde el post_save del movimiento
        # porque aquí sí se sabe QUÉ lo causó: "se resolvió la
        # discrepancia #3 del 28/08, que es anterior a esta" le dice algo
        # a quien revisa; "se registró un movimiento anterior" no. El
        # ajuste lleva la marca _de_resolucion para que la señal no
        # vuelva a recorrer lo mismo con el mensaje genérico.
        marcar_afectadas_por(
            discrepancia.producto_id, conteo.fecha, conteo.ocurrido_en,
            motivo=(
                f"Se resolvió la discrepancia #{discrepancia.pk} del "
                f"{conteo.fecha:%d/%m/%Y}, que es anterior a esta."
            ),
            excluir_id=discrepancia.pk,
        )
    return discrepancia


@transaction.atomic
def descartar_discrepancia(discrepancia, usuario, nota):
    """
    Cierra una discrepancia SIN mover stock: la persona la revisó y
    concluyó que no hay nada que ajustar. Exige nota — sin explicación no
    sirve de nada dentro de seis meses.
    """
    from .models import DiscrepanciaInventario

    if not discrepancia.esta_pendiente:
        raise ValueError("Esta discrepancia ya fue resuelta.")
    if not (nota or "").strip():
        raise ValueError("Descartar una discrepancia exige explicar por qué.")

    discrepancia.estado = DiscrepanciaInventario.DESCARTADA
    discrepancia.cantidad_confirmada = 0
    discrepancia.nota_resolucion = nota
    discrepancia.resuelta_por_id = getattr(usuario, "pk", None)
    discrepancia.resuelta_en = timezone.now()
    discrepancia.requiere_revision = False
    discrepancia.save()
    logger.info(
        "Discrepancia #%d DESCARTADA por %s (sin ajuste): %s",
        discrepancia.pk, getattr(usuario, "username", "?"), nota,
    )
    return discrepancia


# --- Contexto para quien revisa (prompt 34, punto 3) -------------------------

def movimientos_alrededor_del_conteo(discrepancia):
    """
    Todo lo que pasó con ese producto desde el conteo hasta ahora, en el
    ORDEN REAL en que se creó — no en el que llegó al servidor.

    Cada fila trae la relación explicada en palabras ("después del
    conteo"), porque una lista de marcas de tiempo obliga al auditor a
    reconstruir el orden mentalmente, y ahí es justo donde se equivoca:
    una venta posterior al conteo NO explica un faltante, y una anterior
    sí. Esa distinción es la decisión que tiene que tomar.
    """
    from .models import LoteCompra, MovimientoSalida

    conteo = discrepancia.conteo
    corte = (conteo.fecha, conteo.ocurrido_en)
    producto_ids = [discrepancia.producto_id] + list(
        discrepancia.producto.derivados.values_list("pk", flat=True)
    )

    filas = []
    for compra in LoteCompra.objects.filter(producto_id=discrepancia.producto_id, fecha__gte=conteo.fecha):
        filas.append({
            "clase": "entrada", "etiqueta": "Entrada", "objeto": compra,
            "producto": compra.producto, "cantidad": compra.cantidad,
            "fecha": compra.fecha, "ocurrido_en": compra.ocurrido_en,
            "reloj_sospechoso": compra.reloj_sospechoso,
        })
    for salida in (MovimientoSalida.objects
                   .filter(producto_id__in=producto_ids, fecha__gte=conteo.fecha)
                   .select_related("producto")):
        filas.append({
            "clase": salida.tipo, "etiqueta": salida.get_tipo_display(), "objeto": salida,
            "producto": salida.producto, "cantidad": salida.cantidad,
            "fecha": salida.fecha, "ocurrido_en": salida.ocurrido_en,
            "reloj_sospechoso": salida.reloj_sospechoso,
        })

    filas.sort(key=lambda f: (f["fecha"], f["ocurrido_en"]))

    for fila in filas:
        posterior = (fila["fecha"], fila["ocurrido_en"]) > corte
        fila["posterior_al_conteo"] = posterior
        hora = timezone.localtime(fila["ocurrido_en"]).strftime("%H:%M:%S")
        unidades = abs(fila["cantidad"])
        fila["narracion"] = (
            f"{fila['etiqueta']} de {unidades} "
            f"{'unidad' if unidades == 1 else 'unidades'} de {fila['producto']}, "
            f"registrada a las {hora} del {fila['fecha']:%d/%m/%Y}, "
            + ("DESPUÉS del conteo — no explica la diferencia."
               if posterior else
               "ANTES del conteo — ya está incluida en el stock teórico de arriba.")
        )
    return filas

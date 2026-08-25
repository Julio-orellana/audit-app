# inventario/services.py
"""
Lógica financiera de la auditoría: única fuente de verdad para los cálculos
que muestran tanto el dashboard como los reportes Excel (prompt 7).

Funciones puras: reciben parámetros explícitos (producto, fechas), no tocan
`request` ni el usuario autenticado, y no escriben en la base de datos.

Regla de oro: nunca se recalcula costo_unitario_snapshot ni
precio_venta_unitario a partir del costo/precio actual del producto. Se usa
siempre el valor ya guardado en cada MovimientoSalida, que es un snapshot
congelado al momento del registro. Así, un reporte de un mes cerrado da
siempre los mismos números sin importar cuándo se genere.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Sum

from .models import ConteoFisico, LoteCompra, MovimientoSalida, Producto

DOS_DECIMALES = Decimal("0.01")


def _money(valor):
    """Convierte a Decimal y redondea a 2 decimales. None -> 0.00."""
    if valor is None:
        valor = Decimal("0")
    if not isinstance(valor, Decimal):
        valor = Decimal(str(valor))
    return valor.quantize(DOS_DECIMALES)


def _unidades_ajuste_con_signo(ajustes_qs):
    """
    Suma con signo de las unidades de MovimientoSalida tipo='ajuste', para
    reporte: positivo si sobró, negativo si faltó.

    MovimientoSalida.cantidad para un ajuste guarda el efecto real sobre el
    stock (positivo = resta stock = faltante; negativo = suma stock =
    sobrante; ver generar_ajuste() y el help_text del campo). El signo que
    se reporta aquí es el opuesto al guardado, para que sea intuitivo
    ("positivo si sobró") sin alterar cómo se guarda ni cómo afecta
    stock_teorico().
    """
    total = ajustes_qs.aggregate(t=Sum("cantidad"))["t"] or 0
    return -total


def resumen_producto(producto, fecha_inicio, fecha_fin):
    """
    Resumen financiero de un producto en el rango [fecha_inicio, fecha_fin]
    (ambos extremos inclusive). Ver docstring del módulo para las reglas.
    """
    lotes = LoteCompra.objects.filter(
        producto=producto, fecha__gte=fecha_inicio, fecha__lte=fecha_fin
    )
    salidas = MovimientoSalida.objects.filter(
        producto=producto, fecha__gte=fecha_inicio, fecha__lte=fecha_fin
    )

    compras_agg = lotes.aggregate(
        unidades=Sum("cantidad"),
        invertido=Sum(F("cantidad") * F("costo_unitario")),
    )
    unidades_compradas = compras_agg["unidades"] or 0
    invertido = _money(compras_agg["invertido"])

    ventas_agg = salidas.filter(tipo="venta").aggregate(
        unidades=Sum("cantidad"),
        ingreso=Sum(F("cantidad") * F("precio_venta_unitario")),
        costo=Sum(F("cantidad") * F("costo_unitario_snapshot")),
    )
    unidades_vendidas = ventas_agg["unidades"] or 0
    ingreso = _money(ventas_agg["ingreso"])
    costo_de_lo_vendido = _money(ventas_agg["costo"])
    ganancia_bruta = _money(ingreso - costo_de_lo_vendido)

    mermas_agg = salidas.filter(tipo="merma").aggregate(
        unidades=Sum("cantidad"),
        perdida=Sum(F("cantidad") * F("costo_unitario_snapshot")),
    )
    unidades_merma = mermas_agg["unidades"] or 0
    perdida_por_merma = _money(mermas_agg["perdida"])

    unidades_ajuste = _unidades_ajuste_con_signo(salidas.filter(tipo="ajuste"))

    ganancia_neta = _money(ganancia_bruta - perdida_por_merma)

    return {
        "unidades_compradas": unidades_compradas,
        "invertido": invertido,
        "unidades_vendidas": unidades_vendidas,
        "ingreso": ingreso,
        "costo_de_lo_vendido": costo_de_lo_vendido,
        "ganancia_bruta": ganancia_bruta,
        "unidades_merma": unidades_merma,
        "perdida_por_merma": perdida_por_merma,
        "unidades_ajuste": unidades_ajuste,
        "ganancia_neta": ganancia_neta,
        "stock_teorico_al_cierre": producto.stock_teorico(hasta_fecha=fecha_fin),
    }


# Claves que tiene sentido sumar entre productos distintos (montos y
# unidades de movimiento). stock_teorico_al_cierre queda fuera del total
# general a propósito: sumar stock de productos distintos no es una cifra
# financiera útil, así que solo se reporta por producto.
_CLAVES_SUMABLES = (
    "unidades_compradas",
    "invertido",
    "unidades_vendidas",
    "ingreso",
    "costo_de_lo_vendido",
    "ganancia_bruta",
    "unidades_merma",
    "perdida_por_merma",
    "unidades_ajuste",
    "ganancia_neta",
)
_CLAVES_MONETARIAS = ("invertido", "ingreso", "costo_de_lo_vendido", "ganancia_bruta", "perdida_por_merma", "ganancia_neta")


def resumen_general(fecha_inicio, fecha_fin, productos=None):
    """
    Resumen financiero de varios productos en el rango [fecha_inicio,
    fecha_fin]. Si `productos` es None, usa todos los productos activos.

    Devuelve un dict: {"productos": [...], "totales": {...}}
    - "productos": una lista de dicts, cada uno es el resultado de
      resumen_producto() más "producto" (la instancia), "producto_nombre" y
      "categoria" (nombre de categoría).
    - "totales": un dict con la suma de cada clave financiera/de unidades
      (no incluye stock_teorico_al_cierre, ver nota arriba).
    """
    if productos is None:
        productos = Producto.objects.filter(activo=True).select_related("categoria")

    filas = []
    totales = {clave: (Decimal("0.00") if clave in _CLAVES_MONETARIAS else 0) for clave in _CLAVES_SUMABLES}

    for producto in productos:
        resumen = resumen_producto(producto, fecha_inicio, fecha_fin)
        filas.append(
            {
                "producto": producto,
                "producto_nombre": producto.nombre,
                "categoria": producto.categoria.nombre,
                **resumen,
            }
        )
        for clave in _CLAVES_SUMABLES:
            totales[clave] += resumen[clave]

    for clave in _CLAVES_MONETARIAS:
        totales[clave] = _money(totales[clave])

    return {"productos": filas, "totales": totales}


_ETIQUETAS_TIPO_SALIDA = {"venta": "Venta", "merma": "Merma"}


def movimientos_periodo(fecha_inicio, fecha_fin, productos=None, descendente=True):
    """
    Lista combinada y cronológica de TODO lo registrado en el rango
    [fecha_inicio, fecha_fin]: cada LoteCompra ("Entrada"), cada
    MovimientoSalida ("Venta"/"Merma"/"Ajuste (sobrante)"/"Ajuste
    (faltante)") y cada ConteoFisico ("Conteo físico"), sin excepción —
    única fuente de verdad para el historial del dashboard y la hoja
    "Movimientos" del reporte Excel, así el auditor tiene un registro
    completo de todo lo que se hizo, no solo de lo que movió stock.

    Cada fila es un dict: fecha, tipo, producto, cantidad (siempre positiva,
    magnitud), valor_unitario (precio de venta para ventas, costo snapshot
    para merma/ajuste, None para un conteo físico — no aplica), usuario,
    detalle (motivo, notas, o resumen de la diferencia), creado_en.

    `productos`: iterable de Producto para filtrar, o None para todos.
    `descendente`: True = más reciente primero (uso en pantalla); False =
    cronológico ascendente (uso típico en un reporte Excel).
    """
    lotes_qs = LoteCompra.objects.filter(
        fecha__gte=fecha_inicio, fecha__lte=fecha_fin
    ).select_related("producto", "registrado_por")
    salidas_qs = MovimientoSalida.objects.filter(
        fecha__gte=fecha_inicio, fecha__lte=fecha_fin
    ).select_related("producto", "registrado_por")
    conteos_qs = ConteoFisico.objects.filter(
        fecha__gte=fecha_inicio, fecha__lte=fecha_fin
    ).select_related("producto", "registrado_por")

    if productos is not None:
        lotes_qs = lotes_qs.filter(producto__in=productos)
        salidas_qs = salidas_qs.filter(producto__in=productos)
        conteos_qs = conteos_qs.filter(producto__in=productos)

    filas = []
    for lote in lotes_qs:
        detalle = lote.notas or ""
        if lote.proveedor:
            detalle = f"Proveedor: {lote.proveedor}" + (f" · {detalle}" if detalle else "")
        filas.append(
            {
                "fecha": lote.fecha,
                "tipo": "Entrada",
                "producto": lote.producto,
                "cantidad": lote.cantidad,
                "valor_unitario": lote.costo_unitario,
                "usuario": lote.registrado_por.username if lote.registrado_por else "",
                "detalle": detalle,
                "creado_en": lote.creado_en,
            }
        )

    for salida in salidas_qs:
        tipo = _ETIQUETAS_TIPO_SALIDA.get(salida.tipo, "Ajuste")
        cantidad = salida.cantidad
        if salida.tipo == "ajuste":
            # cantidad negativa = sobrante (sumó stock); positiva = faltante.
            if salida.cantidad < 0:
                tipo = "Ajuste (sobrante)"
                cantidad = abs(salida.cantidad)
            else:
                tipo = "Ajuste (faltante)"

        valor_unitario = salida.precio_venta_unitario if salida.tipo == "venta" else salida.costo_unitario_snapshot

        filas.append(
            {
                "fecha": salida.fecha,
                "tipo": tipo,
                "producto": salida.producto,
                "cantidad": cantidad,
                "valor_unitario": valor_unitario,
                "usuario": salida.registrado_por.username if salida.registrado_por else "",
                "detalle": salida.motivo or "",
                "creado_en": salida.creado_en,
            }
        )

    for conteo in conteos_qs:
        diferencia = conteo.diferencia
        detalle = f"Contado: {conteo.cantidad_contada} · Diferencia: {diferencia:+d}"
        if conteo.ajuste_generado_id:
            detalle += " · ajuste generado"
        elif diferencia != 0:
            detalle += " · pendiente de ajuste"
        if conteo.notas:
            detalle += f" · {conteo.notas}"

        filas.append(
            {
                "fecha": conteo.fecha,
                "tipo": "Conteo físico",
                "producto": conteo.producto,
                "cantidad": conteo.cantidad_contada,
                "valor_unitario": None,
                "usuario": conteo.registrado_por.username if conteo.registrado_por else "",
                "detalle": detalle,
                "creado_en": conteo.creado_en,
            }
        )

    filas.sort(key=lambda f: (f["fecha"], f["creado_en"]), reverse=descendente)
    return filas


def serie_diaria_ventas(fecha_inicio, fecha_fin, productos=None):
    """
    Ingreso por ventas (snapshot, nunca recalculado) sumado por día, para
    cada día del rango [fecha_inicio, fecha_fin] (incluye días sin ventas
    con 0.00). Única fuente de verdad para la línea de tendencia, tanto en
    pantalla (dashboard/reportes) como en el Excel.
    """
    qs = MovimientoSalida.objects.filter(tipo="venta", fecha__gte=fecha_inicio, fecha__lte=fecha_fin)
    if productos is not None:
        qs = qs.filter(producto__in=productos)
    agregados = qs.values("fecha").annotate(ingreso=Sum(F("cantidad") * F("precio_venta_unitario")))
    ingreso_por_fecha = {fila["fecha"]: _money(fila["ingreso"]) for fila in agregados}

    serie = []
    cursor = fecha_inicio
    while cursor <= fecha_fin:
        serie.append((cursor, ingreso_por_fecha.get(cursor, Decimal("0.00"))))
        cursor += timedelta(days=1)
    return serie


def alertas_conteo_fisico():
    """
    Productos activos con AL MENOS UN ConteoFisico sin resolver: diferencia
    distinta de 0 y sin ajuste_generado.

    Importante: antes esta función solo miraba el ÚLTIMO ConteoFisico de
    cada producto. Eso significaba que si se registraba un conteo nuevo
    para un producto que ya tenía un conteo anterior sin resolver, el
    anterior desaparecía de las alertas sin que nadie lo hubiera aprobado
    y sin que el stock se hubiera movido — parecía que el sistema lo había
    resuelto solo. Ahora se revisan TODOS los conteos del producto, así
    que ningún conteo sin ajuste queda invisible.

    Devuelve una lista de dicts: {"producto", "conteo", "diferencia",
    "total_pendientes"}.
    - "conteo": el conteo sin resolver MÁS ANTIGUO de ese producto (para
      animar a resolverlos en el orden en que ocurrieron).
    - "total_pendientes": cuántos conteos sin resolver tiene ese producto
      en total (normalmente 1; más de 1 solo si se dejaron varios conteos
      sin generar su ajuste antes de seguir contando).
    Ordenados por fecha del conteo pendiente más antiguo (más urgente primero).
    """
    alertas = []
    for producto in Producto.objects.filter(activo=True):
        pendientes = [
            conteo
            for conteo in producto.conteos_fisicos.order_by("fecha", "creado_en")
            if conteo.ajuste_generado_id is None and conteo.diferencia != 0
        ]
        if not pendientes:
            continue
        conteo_mas_antiguo = pendientes[0]
        alertas.append(
            {
                "producto": producto,
                "conteo": conteo_mas_antiguo,
                "diferencia": conteo_mas_antiguo.diferencia,
                "total_pendientes": len(pendientes),
            }
        )

    alertas.sort(key=lambda a: a["conteo"].fecha)
    return alertas

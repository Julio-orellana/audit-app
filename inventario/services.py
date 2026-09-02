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
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import F, Sum

from .models import ConteoFisico, LoteCompra, MovimientoSalida, Producto

DOS_DECIMALES = Decimal("0.01")


class MotorStockCosto:
    """
    Calcula stock_teorico() y costo_promedio() de CUALQUIER cantidad de
    productos, cada uno a su propia fecha de corte si hace falta, con una
    cantidad FIJA de 3 consultas sin importar cuántos productos,
    movimientos o fechas de corte distintas se pidan después (prompt 18b
    — el dashboard, Reportes e Historial disparaban una consulta por
    producto/conteo antes de esto).

    Se construye una sola vez por request: trae TODOS los productos
    (id, producto_base_id, factor_equivalencia) y TODAS las filas de
    LoteCompra/MovimientoSalida del sistema como (producto_id, fecha,
    cantidad[, costo_unitario]) — 3 consultas — y a partir de ahí calcula
    cualquier stock_teorico(producto_id, hasta_fecha)/costo_promedio(...)
    en memoria, sin volver a tocar la base de datos.

    Por qué se trae TODO (no solo los productos/fechas que se van a pedir
    hoy): un producto derivado necesita el stock de su base aunque esa
    base no esté en la lista pedida (ej. un producto inactivo), y un
    conteo físico puede pedir stock a una fecha de corte arbitraria y
    distinta para cada uno (Historial). Filtrar de antemano exigiría
    adivinar qué se va a necesitar; traer todo es 1 consulta más grande
    en vez de N consultas chicas, y para el volumen de datos real de esta
    app (un restaurante, decenas de productos) es información que cabe
    cómoda en memoria — mucho más barato que una consulta por producto
    contra una base remota.

    Reproduce EXACTAMENTE la misma lógica que Producto.stock_teorico()/
    costo_promedio() (ver models.py), incluida la resolución de productos
    derivados del prompt 15 — verificado método por método contra la
    versión original antes de reemplazarla en las vistas (ver
    inventario/tests.py, ParidadMotorStockCostoTests).
    """

    def __init__(self):
        self._mapa = {
            p["id"]: (p["producto_base_id"], p["factor_equivalencia"])
            for p in Producto.objects.values("id", "producto_base_id", "factor_equivalencia")
        }
        self._derivados_por_base = defaultdict(list)
        for pid, (base_id, factor) in self._mapa.items():
            if base_id is not None:
                self._derivados_por_base[base_id].append((pid, factor))

        self._compras_por_producto = defaultdict(list)
        for producto_id, fecha, cantidad, costo_unitario in LoteCompra.objects.values_list(
            "producto_id", "fecha", "cantidad", "costo_unitario"
        ):
            self._compras_por_producto[producto_id].append((fecha, cantidad, costo_unitario))

        self._salidas_por_producto = defaultdict(list)
        for producto_id, fecha, cantidad in MovimientoSalida.objects.values_list("producto_id", "fecha", "cantidad"):
            self._salidas_por_producto[producto_id].append((fecha, cantidad))

    def _suma_hasta(self, filas, hasta_fecha, indice_cantidad=1):
        if hasta_fecha is None:
            return sum(fila[indice_cantidad] for fila in filas)
        return sum(fila[indice_cantidad] for fila in filas if fila[0] <= hasta_fecha)

    def stock_teorico(self, producto_id, hasta_fecha=None):
        base_id, factor = self._mapa[producto_id]
        if base_id is not None:
            return self.stock_teorico(base_id, hasta_fecha) // factor

        total_compras = self._suma_hasta(self._compras_por_producto.get(producto_id, []), hasta_fecha)
        total_salidas = self._suma_hasta(self._salidas_por_producto.get(producto_id, []), hasta_fecha)

        total_salidas_derivados = 0
        for derivado_id, derivado_factor in self._derivados_por_base.get(producto_id, []):
            cantidad_derivado = self._suma_hasta(self._salidas_por_producto.get(derivado_id, []), hasta_fecha)
            total_salidas_derivados += cantidad_derivado * derivado_factor

        return total_compras - total_salidas - total_salidas_derivados

    def costo_promedio(self, producto_id, hasta_fecha=None):
        base_id, factor = self._mapa[producto_id]
        if base_id is not None:
            return (self.costo_promedio(base_id, hasta_fecha) * factor).quantize(DOS_DECIMALES)

        filas = self._compras_por_producto.get(producto_id, [])
        if hasta_fecha is not None:
            filas = [f for f in filas if f[0] <= hasta_fecha]
        total_unidades = sum(cantidad for _fecha, cantidad, _costo in filas)
        if not total_unidades:
            return Decimal("0.00")
        total_costo = sum(cantidad * costo for _fecha, cantidad, costo in filas)
        return (total_costo / total_unidades).quantize(DOS_DECIMALES)


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


def _perdida_por_ajuste_faltante(ajustes_qs):
    """
    Costo del inventario que un ajuste confirmado dio por perdido.

    Solo cuenta los FALTANTES. En MovimientoSalida.cantidad un ajuste
    guarda su efecto real sobre el stock, así que cantidad > 0 es "resta
    stock", o sea faltó producto: eso es una pérdida real, con su costo,
    y resta de la ganancia neta igual que una merma (prompt 34b).

    Un sobrante (cantidad < 0) se deja fuera a propósito. No es una venta
    realizada; sumarlo como ganancia inflaría el resultado del período por
    algo que solo significa que el registro estaba mal. Se reporta aparte,
    en unidades_ajuste, que sí lleva los dos signos.
    """
    agregado = ajustes_qs.filter(cantidad__gt=0).aggregate(
        perdida=Sum(F("cantidad") * F("costo_unitario_snapshot"))
    )
    return _money(agregado["perdida"])


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

    ajustes = salidas.filter(tipo="ajuste")
    unidades_ajuste = _unidades_ajuste_con_signo(ajustes)
    perdida_por_ajuste = _perdida_por_ajuste_faltante(ajustes)

    # Un FALTANTE confirmado resta de la ganancia igual que una merma
    # (prompt 34b): es inventario real que ya no está, con su costo, sin
    # importar la causa. Un SOBRANTE no suma nada — no es una venta
    # realizada, y contarlo como ganancia distorsionaría el reporte en la
    # dirección contraria. Queda visible en unidades_ajuste, que es
    # informativo y lleva los dos signos.
    ganancia_neta = _money(ganancia_bruta - perdida_por_merma - perdida_por_ajuste)

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
        "perdida_por_ajuste": perdida_por_ajuste,
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
    "perdida_por_ajuste",
    "ganancia_neta",
)
_CLAVES_MONETARIAS = (
    "invertido", "ingreso", "costo_de_lo_vendido", "ganancia_bruta",
    "perdida_por_merma", "perdida_por_ajuste", "ganancia_neta",
)


def resumen_general(fecha_inicio, fecha_fin, productos=None, motor=None):
    """
    Resumen financiero de varios productos en el rango [fecha_inicio,
    fecha_fin]. Si `productos` es None, usa todos los productos activos.

    `motor`: un MotorStockCosto ya construido, para compartirlo con quien
    llama en vez de construir uno nuevo (prompt 24) — home() ya arma el
    suyo propio para las alertas/último conteo de la página, y llamar
    aquí sin pasarlo construía un SEGUNDO MotorStockCosto completo,
    repitiendo sus 3 consultas fijas sin necesidad. Cada consulta de más
    contra Neon paga la misma latencia de red fija que cualquier otra
    (~80-100ms medido), así que evitar 3 de más sí se siente.

    Devuelve un dict: {"productos": [...], "totales": {...}}
    - "productos": una lista de dicts, con las mismas claves que devolvía
      resumen_producto() (calculadas aquí en lote, no llamándolo por
      producto — ver nota de rendimiento abajo) más "producto" (la
      instancia), "producto_nombre" y "categoria" (nombre de categoría).
    - "totales": un dict con la suma de cada clave financiera/de unidades
      (no incluye stock_teorico_al_cierre, ver nota arriba).

    Rendimiento (prompt 18b): antes llamaba resumen_producto() —y por lo
    tanto sus propios .aggregate()+stock_teorico()— una vez POR PRODUCTO,
    disparando una cantidad de consultas proporcional a la cantidad de
    productos (219 consultas con 26 productos, medido en el prompt 20).
    Aquí se calculan las 4 agregaciones financieras (compras, ventas,
    mermas, ajustes) de TODOS los productos del rango en una sola
    consulta cada una, agrupadas por producto_id — y el stock al cierre
    con un único MotorStockCosto compartido — así el total de consultas
    queda fijo (8) sin importar cuántos productos se incluyan. Los
    números que devuelve son idénticos a los de resumen_producto() (ver
    inventario/tests.py, ParidadResumenGeneralTests) — solo cambia cómo
    se calculan, nunca el resultado.
    """
    if productos is None:
        productos = list(Producto.objects.filter(activo=True).select_related("categoria"))
    else:
        # Se vuelve a consultar con select_related("categoria") así el
        # acceso a producto.categoria.nombre más abajo no dispare una
        # consulta por producto cuando `productos` viene de afuera (ej.
        # la selección específica del formulario de Reportes) sin
        # traerla ya resuelta.
        productos = list(
            Producto.objects.filter(pk__in=[p.pk for p in productos]).select_related("categoria")
        )

    producto_ids = [p.pk for p in productos]

    def _agrupar_por_producto(queryset, **anotaciones):
        return {
            fila["producto_id"]: fila
            for fila in queryset.filter(producto_id__in=producto_ids).values("producto_id").annotate(**anotaciones)
        }

    compras_por_producto = _agrupar_por_producto(
        LoteCompra.objects.filter(fecha__gte=fecha_inicio, fecha__lte=fecha_fin),
        unidades=Sum("cantidad"),
        invertido=Sum(F("cantidad") * F("costo_unitario")),
    )
    ventas_por_producto = _agrupar_por_producto(
        MovimientoSalida.objects.filter(fecha__gte=fecha_inicio, fecha__lte=fecha_fin, tipo="venta"),
        unidades=Sum("cantidad"),
        ingreso=Sum(F("cantidad") * F("precio_venta_unitario")),
        costo=Sum(F("cantidad") * F("costo_unitario_snapshot")),
    )
    mermas_por_producto = _agrupar_por_producto(
        MovimientoSalida.objects.filter(fecha__gte=fecha_inicio, fecha__lte=fecha_fin, tipo="merma"),
        unidades=Sum("cantidad"),
        perdida=Sum(F("cantidad") * F("costo_unitario_snapshot")),
    )
    # Mismo signo que _unidades_ajuste_con_signo(): positivo si sobró.
    ajustes_qs = MovimientoSalida.objects.filter(
        producto_id__in=producto_ids, fecha__gte=fecha_inicio, fecha__lte=fecha_fin, tipo="ajuste"
    )
    ajustes_por_producto = {
        fila["producto_id"]: -(fila["total"] or 0)
        for fila in ajustes_qs.values("producto_id").annotate(total=Sum("cantidad"))
    }
    # Solo los faltantes (cantidad > 0 = resta stock) pesan en la ganancia
    # (prompt 34b). Tiene que calcularse igual que en resumen_producto():
    # las dos implementaciones existen por rendimiento y ParidadResumen
    # GeneralTests exige que den exactamente lo mismo.
    perdida_ajuste_por_producto = {
        fila["producto_id"]: _money(fila["perdida"])
        for fila in ajustes_qs.filter(cantidad__gt=0)
        .values("producto_id")
        .annotate(perdida=Sum(F("cantidad") * F("costo_unitario_snapshot")))
    }

    motor = motor or MotorStockCosto()

    filas = []
    totales = {clave: (Decimal("0.00") if clave in _CLAVES_MONETARIAS else 0) for clave in _CLAVES_SUMABLES}

    for producto in productos:
        compras = compras_por_producto.get(producto.pk, {})
        ventas = ventas_por_producto.get(producto.pk, {})
        mermas = mermas_por_producto.get(producto.pk, {})

        ingreso = _money(ventas.get("ingreso"))
        costo_de_lo_vendido = _money(ventas.get("costo"))
        ganancia_bruta = _money(ingreso - costo_de_lo_vendido)
        perdida_por_merma = _money(mermas.get("perdida"))
        perdida_por_ajuste = perdida_ajuste_por_producto.get(producto.pk, Decimal("0.00"))
        ganancia_neta = _money(ganancia_bruta - perdida_por_merma - perdida_por_ajuste)

        resumen = {
            "unidades_compradas": compras.get("unidades") or 0,
            "invertido": _money(compras.get("invertido")),
            "unidades_vendidas": ventas.get("unidades") or 0,
            "ingreso": ingreso,
            "costo_de_lo_vendido": costo_de_lo_vendido,
            "ganancia_bruta": ganancia_bruta,
            "unidades_merma": mermas.get("unidades") or 0,
            "perdida_por_merma": perdida_por_merma,
            "unidades_ajuste": ajustes_por_producto.get(producto.pk, 0),
            "perdida_por_ajuste": perdida_por_ajuste,
            "ganancia_neta": ganancia_neta,
            "stock_teorico_al_cierre": motor.stock_teorico(producto.pk, hasta_fecha=fecha_fin),
        }
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

# Códigos canónicos de "tipo de movimiento" (prompt 35), independientes
# del texto que se muestra en pantalla (ese puede llevar acentos,
# paréntesis, etc. — no sirve como valor de filtro estable). Un ajuste
# CONFIRMADO (ya existe como MovimientoSalida) y una discrepancia
# PENDIENTE (todavía ni tiene ajuste) son dos códigos distintos a
# propósito: son dos momentos distintos de la misma historia, y quien
# filtra por uno no quiere ver el otro mezclado — ver el docstring de
# movimientos_periodo() para el detalle de cada caso.
TIPO_ENTRADA = "entrada"
TIPO_VENTA = "venta"
TIPO_MERMA = "merma"
TIPO_AJUSTE_SOBRANTE = "ajuste_sobrante"
TIPO_AJUSTE_FALTANTE = "ajuste_faltante"
TIPO_CONTEO = "conteo"
TIPO_DISCREPANCIA_PENDIENTE = "discrepancia_pendiente"
TIPO_DISCREPANCIA_RESUELTA = "discrepancia_resuelta"
TIPO_DISCREPANCIA_DESCARTADA = "discrepancia_descartada"
# Nota automática (prompt 37): constancia de un conflicto de orden que el
# sistema detectó y descartó por no cambiar ningún resultado. No es un
# movimiento —no mueve stock ni dinero— pero vive en el Historial porque
# es justo ahí donde explica por qué el orden es el que es.
TIPO_NOTA_AUTOMATICA = "nota_automatica"

# Tope de notas automáticas que se traen al Historial. Son constancias
# informativas, no movimientos: si algún día hubiera miles (una tienda
# trabajando semanas sin conexión), no deben desplazar del Historial a lo
# que de verdad movió inventario. Las que queden fuera siguen completas
# en la pantalla de Correcciones, que es su lugar natural.
LIMITE_NOTAS_HISTORIAL = 500

# Los 3 estados de discrepancia se exponen tal cual —ni más ni menos—
# porque son justo los que ya distingue DiscrepanciaInventario.ESTADOS
# (prompt 34): no es un filtro nuevo inventado, es el mismo estado ya
# modelado, solo hecho filtrable.
OPCIONES_TIPO_MOVIMIENTO = [
    ("", "Todos los tipos"),
    (TIPO_ENTRADA, "Entrada"),
    (TIPO_VENTA, "Venta"),
    (TIPO_MERMA, "Merma"),
    (TIPO_AJUSTE_SOBRANTE, "Ajuste (sobrante)"),
    (TIPO_AJUSTE_FALTANTE, "Ajuste (faltante)"),
    (TIPO_CONTEO, "Conteo físico (todos)"),
    (TIPO_DISCREPANCIA_PENDIENTE, "· Discrepancia pendiente de revisión"),
    (TIPO_DISCREPANCIA_RESUELTA, "· Discrepancia resuelta"),
    (TIPO_DISCREPANCIA_DESCARTADA, "· Discrepancia descartada sin ajuste"),
    (TIPO_NOTA_AUTOMATICA, "Nota automática del sistema"),
]


def movimientos_periodo(
    fecha_inicio, fecha_fin, productos=None, descendente=True,
    tipo_movimiento=None, usuario_id=None,
):
    """
    Lista combinada y cronológica de TODO lo registrado en el rango
    [fecha_inicio, fecha_fin]: cada LoteCompra ("Entrada"), cada
    MovimientoSalida ("Venta"/"Merma"/"Ajuste (sobrante)"/"Ajuste
    (faltante)") y cada ConteoFisico ("Conteo físico"), sin excepción —
    única fuente de verdad para el historial del dashboard y la hoja
    "Movimientos" del reporte Excel, así el auditor tiene un registro
    completo de todo lo que se hizo, no solo de lo que movió stock.

    Cada fila es un dict: fecha, tipo (texto para mostrar), tipo_codigo
    (uno de TIPO_*, para filtrar), producto, cantidad (siempre positiva,
    magnitud), valor_unitario (precio de venta para ventas, costo snapshot
    para merma/ajuste, None para un conteo físico — no aplica), usuario,
    usuario_id, detalle (motivo, notas, o resumen de la diferencia),
    creado_en (cuándo se insertó en la base — hora de sincronización para
    algo que vino de la cola offline), ocurrido_en (prompt 34: el instante
    real en que la persona lo registró en su equipo — la fecha que
    importa para el orden cronológico, ver el sort al final).

    `productos`: iterable de Producto para filtrar, o None para todos.
    `descendente`: True = más reciente primero (uso en pantalla); False =
    cronológico ascendente (uso típico en un reporte Excel).
    `tipo_movimiento`: uno de los TIPO_* de arriba, o None/"" para todos
    (prompt 35). Decide de entrada CUÁLES de las tres tablas hace falta
    siquiera consultar — pedir solo "venta" no toca LoteCompra ni
    ConteoFisico en absoluto, ni un query de más.
    `usuario_id`: pk de auth.User para filtrar por quien registró, o None
    para todos (prompt 35).

    Un ajuste CONFIRMADO (tipo_codigo="ajuste_sobrante"/"ajuste_faltante")
    y una discrepancia PENDIENTE (tipo_codigo="discrepancia_pendiente")
    son cosas DISTINTAS aunque nazcan del mismo conteo: la primera es un
    MovimientoSalida real que ya movió stock; la segunda es el
    ConteoFisico esperando revisión, con CERO movimiento de stock detrás
    (ver inventario/discrepancias.py). Filtrar por "ajuste" nunca debe
    traer de vuelta algo que todavía está pendiente, y viceversa — es
    justo la distinción que este prompt pide mantener.
    """
    from .models import DiscrepanciaInventario

    incluir_lotes = tipo_movimiento in (None, "", TIPO_ENTRADA)
    incluir_ventas = tipo_movimiento in (None, "", TIPO_VENTA)
    incluir_mermas = tipo_movimiento in (None, "", TIPO_MERMA)
    incluir_ajuste_sobrante = tipo_movimiento in (None, "", TIPO_AJUSTE_SOBRANTE)
    incluir_ajuste_faltante = tipo_movimiento in (None, "", TIPO_AJUSTE_FALTANTE)
    incluir_conteos = tipo_movimiento in (
        None, "", TIPO_CONTEO,
        TIPO_DISCREPANCIA_PENDIENTE, TIPO_DISCREPANCIA_RESUELTA, TIPO_DISCREPANCIA_DESCARTADA,
    )
    incluir_notas = tipo_movimiento in (None, "", TIPO_NOTA_AUTOMATICA)

    lotes_qs = LoteCompra.objects.none()
    if incluir_lotes:
        lotes_qs = LoteCompra.objects.filter(
            fecha__gte=fecha_inicio, fecha__lte=fecha_fin
        ).select_related("producto", "registrado_por")
        if usuario_id:
            lotes_qs = lotes_qs.filter(registrado_por_id=usuario_id)

    salidas_qs = MovimientoSalida.objects.none()
    if incluir_ventas or incluir_mermas or incluir_ajuste_sobrante or incluir_ajuste_faltante:
        salidas_qs = MovimientoSalida.objects.filter(
            fecha__gte=fecha_inicio, fecha__lte=fecha_fin
        ).select_related("producto", "registrado_por")
        if usuario_id:
            salidas_qs = salidas_qs.filter(registrado_por_id=usuario_id)
        # Ventas/merma se distinguen por MovimientoSalida.tipo; sobrante
        # vs. faltante son el MISMO tipo="ajuste" partido por el signo de
        # cantidad (ver help_text de MovimientoSalida.cantidad) — no hay
        # forma de pedir "solo sobrante" sin filtrar por cantidad también.
        if tipo_movimiento == TIPO_VENTA:
            salidas_qs = salidas_qs.filter(tipo="venta")
        elif tipo_movimiento == TIPO_MERMA:
            salidas_qs = salidas_qs.filter(tipo="merma")
        elif tipo_movimiento == TIPO_AJUSTE_SOBRANTE:
            salidas_qs = salidas_qs.filter(tipo="ajuste", cantidad__lt=0)
        elif tipo_movimiento == TIPO_AJUSTE_FALTANTE:
            salidas_qs = salidas_qs.filter(tipo="ajuste", cantidad__gt=0)
        elif not tipo_movimiento:
            pass  # sin filtro de tipo: venta + merma + ajuste, las tres
        else:
            # tipo_movimiento pedía algo que no es una MovimientoSalida
            # (p. ej. "conteo"): no traer NADA de esta tabla.
            salidas_qs = MovimientoSalida.objects.none()

    conteos_qs = ConteoFisico.objects.none()
    if incluir_conteos:
        conteos_qs = ConteoFisico.objects.filter(
            fecha__gte=fecha_inicio, fecha__lte=fecha_fin
        ).select_related("producto", "registrado_por")
        if usuario_id:
            conteos_qs = conteos_qs.filter(registrado_por_id=usuario_id)
        # El JOIN contra la discrepancia va a nivel de queryset —no se
        # traen conteos de más para descartarlos después en Python—
        # usando el related_name="discrepancia" de la OneToOneField.
        if tipo_movimiento == TIPO_DISCREPANCIA_PENDIENTE:
            conteos_qs = conteos_qs.filter(discrepancia__estado=DiscrepanciaInventario.PENDIENTE)
        elif tipo_movimiento == TIPO_DISCREPANCIA_RESUELTA:
            conteos_qs = conteos_qs.filter(discrepancia__estado=DiscrepanciaInventario.RESUELTA)
        elif tipo_movimiento == TIPO_DISCREPANCIA_DESCARTADA:
            conteos_qs = conteos_qs.filter(discrepancia__estado=DiscrepanciaInventario.DESCARTADA)

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
                "tipo_codigo": TIPO_ENTRADA,
                "producto": lote.producto,
                "cantidad": lote.cantidad,
                "valor_unitario": lote.costo_unitario,
                "usuario": lote.registrado_por.username if lote.registrado_por else "",
                "usuario_id": lote.registrado_por_id,
                "detalle": detalle,
                "creado_en": lote.creado_en,
                "ocurrido_en": lote.ocurrido_en,
                "tipo_registro": "LoteCompra",
                "registro_id": lote.pk,
            }
        )

    for salida in salidas_qs:
        tipo = _ETIQUETAS_TIPO_SALIDA.get(salida.tipo, "Ajuste")
        tipo_codigo = {"venta": TIPO_VENTA, "merma": TIPO_MERMA}.get(salida.tipo)
        cantidad = salida.cantidad
        if salida.tipo == "ajuste":
            # cantidad negativa = sobrante (sumó stock); positiva = faltante.
            if salida.cantidad < 0:
                tipo = "Ajuste (sobrante)"
                tipo_codigo = TIPO_AJUSTE_SOBRANTE
                cantidad = abs(salida.cantidad)
            else:
                tipo = "Ajuste (faltante)"
                tipo_codigo = TIPO_AJUSTE_FALTANTE

        valor_unitario = salida.precio_venta_unitario if salida.tipo == "venta" else salida.costo_unitario_snapshot

        filas.append(
            {
                "fecha": salida.fecha,
                "tipo": tipo,
                "tipo_codigo": tipo_codigo,
                "producto": salida.producto,
                "cantidad": cantidad,
                "valor_unitario": valor_unitario,
                "usuario": salida.registrado_por.username if salida.registrado_por else "",
                "usuario_id": salida.registrado_por_id,
                "detalle": salida.motivo or "",
                "creado_en": salida.creado_en,
                "ocurrido_en": salida.ocurrido_en,
                "tipo_registro": "MovimientoSalida",
                "registro_id": salida.pk,
            }
        )

    conteos = list(conteos_qs)
    if conteos:
        # La diferencia sale de la DiscrepanciaInventario congelada, no de
        # una resta contra el stock actual (prompt 34). Antes se
        # recalculaba aquí con hasta_fecha, y el resultado era engañoso en
        # cuanto llegaba cualquier movimiento posterior: comprobado en el
        # Excel de este mismo prompt, un conteo cuya diferencia real era
        # −5 aparecía como "+7" después de aplicarle su propio ajuste,
        # porque la resta se hacía contra el stock ya corregido. Un número
        # sin significado en un reporte financiero.
        discrepancias = {
            d.conteo_id: d
            for d in DiscrepanciaInventario.objects.filter(conteo__in=conteos)
        }
        for conteo in conteos:
            discrepancia = discrepancias.get(conteo.pk)
            if discrepancia is None:
                # Sin discrepancia registrada el conteo cuadró exacto —o
                # es anterior al prompt 34, y entonces no hay un número
                # confiable que mostrar.
                detalle = f"Contado: {conteo.cantidad_contada}"
                tipo_codigo = TIPO_CONTEO
            else:
                detalle = (
                    f"Contado: {conteo.cantidad_contada} · "
                    f"Diferencia: {discrepancia.diferencia:+d}"
                )
                if discrepancia.estado == DiscrepanciaInventario.RESUELTA:
                    detalle += f" · ajuste aplicado ({discrepancia.cantidad_confirmada:+d})"
                    tipo_codigo = TIPO_DISCREPANCIA_RESUELTA
                elif discrepancia.estado == DiscrepanciaInventario.DESCARTADA:
                    detalle += " · revisada sin ajuste"
                    tipo_codigo = TIPO_DISCREPANCIA_DESCARTADA
                else:
                    detalle += " · pendiente de revisión"
                    tipo_codigo = TIPO_DISCREPANCIA_PENDIENTE
            if conteo.notas:
                detalle += f" · {conteo.notas}"

            filas.append(
                {
                    "fecha": conteo.fecha,
                    "tipo": "Conteo físico",
                    "tipo_codigo": tipo_codigo,
                    "producto": conteo.producto,
                    "cantidad": conteo.cantidad_contada,
                    "valor_unitario": None,
                    "usuario": conteo.registrado_por.username if conteo.registrado_por else "",
                    "usuario_id": conteo.registrado_por_id,
                    "detalle": detalle,
                    "creado_en": conteo.creado_en,
                    "ocurrido_en": conteo.ocurrido_en,
                    "tipo_registro": "ConteoFisico",
                    "registro_id": conteo.pk,
                }
            )

    if incluir_notas:
        filas.extend(_filas_de_notas_automaticas(fecha_inicio, fecha_fin, productos, usuario_id))

    # ocurrido_en, no creado_en (prompt 35): el orden cronológico real es
    # cuándo se registró en el equipo de quien lo hizo, no cuándo llegó a
    # la base. Con creado_en, un movimiento cargado sin conexión y
    # sincronizado horas después aparecía al FINAL de la lista según la
    # hora de sincronización, en vez de en el lugar que le correspondía
    # según cuándo ocurrió de verdad — ver AnclaTemporalMixin (prompt 34).
    filas.sort(key=lambda f: (f["fecha"], f["ocurrido_en"]), reverse=descendente)
    return filas


def _filas_de_notas_automaticas(fecha_inicio, fecha_fin, productos=None, usuario_id=None):
    """
    Las notas automáticas del sistema como filas de Historial (prompt 37).

    No son movimientos: no mueven stock ni dinero, y por eso no tienen
    cantidad ni valor. Están en el Historial porque es exactamente ahí
    donde hacen falta — explican por qué un movimiento aparece donde
    aparece cuando llegó fuera de orden.

    `usuario_id`: una nota la escribe el SISTEMA, no una persona
    (realizado_por es NULL), así que filtrar por usuario las excluye
    siempre. Es lo correcto: quien busca "todo lo de Ruth" no está
    buscando notas que Ruth no escribió.
    """
    from .models import CorreccionHistorial, Producto

    if usuario_id:
        return []

    notas = list(
        CorreccionHistorial.objects
        .filter(accion=CorreccionHistorial.ACCION_NOTA)
        .order_by("-fecha")[:LIMITE_NOTAS_HISTORIAL]
    )
    if not notas:
        return []

    ids_producto = {n.datos_nuevos.get("producto_id") for n in notas if n.datos_nuevos}
    ids_producto.discard(None)
    productos_por_id = {p.pk: p for p in Producto.objects.filter(pk__in=ids_producto)}
    ids_permitidos = {p.pk for p in productos} if productos is not None else None

    filas = []
    for nota in notas:
        datos = nota.datos_nuevos or {}
        producto = productos_por_id.get(datos.get("producto_id"))
        if producto is None:
            continue
        if ids_permitidos is not None and producto.pk not in ids_permitidos:
            continue
        try:
            fecha = date.fromisoformat(datos["fecha"])
            ocurrido_en = datetime.fromisoformat(datos["ocurrido_en"])
        except (KeyError, TypeError, ValueError):
            # Nota escrita por una versión anterior, sin estos campos: se
            # omite del Historial en vez de reventarlo. Sigue visible en
            # la pantalla de Correcciones.
            continue
        if not (fecha_inicio <= fecha <= fecha_fin):
            continue

        filas.append(
            {
                "fecha": fecha,
                "tipo": "Nota automática",
                "tipo_codigo": TIPO_NOTA_AUTOMATICA,
                "producto": producto,
                "cantidad": 0,
                "valor_unitario": None,
                "usuario": "",          # la escribió el sistema
                "usuario_id": None,
                "detalle": nota.motivo,
                "creado_en": nota.fecha,
                "ocurrido_en": ocurrido_en,
                "tipo_registro": "CorreccionHistorial",
                "registro_id": nota.pk,
            }
        )
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


def snapshot_registro(instance):
    """
    Representación JSON-serializable de un LoteCompra/MovimientoSalida/
    ConteoFisico, para guardar en CorreccionHistorial.datos_anteriores /
    datos_nuevos (prompt 17). Incluye el valor crudo de cada campo (una
    FK queda como su id, igual que en la base de datos) más el nombre del
    producto relacionado en texto plano, para que la vista de
    correcciones siga siendo legible aunque ese producto se renombre o
    se desactive más adelante.
    """
    datos = {}
    for field in instance._meta.fields:
        valor = field.value_from_object(instance)
        if isinstance(valor, Decimal):
            valor = str(valor)
        elif isinstance(valor, (datetime, date)):
            valor = valor.isoformat()
        elif isinstance(valor, uuid.UUID):
            # Campo "uuid" (prompt 19) — mismo motivo que Decimal/fecha:
            # json.dumps() no sabe serializar un UUID directamente.
            valor = str(valor)
        datos[field.name] = valor
    if getattr(instance, "producto_id", None):
        datos["producto_nombre"] = instance.producto.nombre
    if getattr(instance, "registrado_por_id", None):
        datos["registrado_por_username"] = instance.registrado_por.username
    return datos


def productos_activos_por_categoria(alias="default"):
    """
    Productos activos agrupados por categoría, en orden — misma agrupación
    que usa el selector de productos de Reportes y el catálogo reducido
    del dashboard de un vendedor (ver home_vendedor.html).

    `alias`: "default" (Neon) en el uso normal; "local_disco" (prompt 19)
    cuando se llama para el catálogo cacheado sin conexión — Reportes
    nunca pasa "local_disco", siempre requiere conexión activa.

    Devuelve una lista de (nombre_categoria, [productos]).
    """
    productos = (
        Producto.objects.using(alias)
        .filter(activo=True)
        .select_related("categoria")
        .order_by("categoria__nombre", "nombre")
    )
    grupos = {}
    orden = []
    for producto in productos:
        nombre_cat = producto.categoria.nombre
        if nombre_cat not in grupos:
            grupos[nombre_cat] = []
            orden.append(nombre_cat)
        grupos[nombre_cat].append(producto)
    return [(nombre, grupos[nombre]) for nombre in orden]


def conteos_activos_por_producto():
    """
    Todos los ConteoFisico de productos ACTIVOS, agrupados por
    producto_id y ordenados de más antiguo a más reciente — 1 sola
    consulta. Compartida entre alertas_conteo_fisico() y home() (que
    también necesita el último conteo de cada producto), para no repetir
    la misma consulta dos veces en el mismo request.
    """
    agrupado = defaultdict(list)
    conteos = (
        ConteoFisico.objects.filter(producto__activo=True)
        .select_related("producto", "registrado_por")
        .order_by("producto_id", "fecha", "creado_en")
    )
    for conteo in conteos:
        agrupado[conteo.producto_id].append(conteo)
    return agrupado


def alertas_conteo_fisico(motor=None, conteos_por_producto=None):
    """
    Discrepancias PENDIENTES de revisión, para el tablero.

    Desde el prompt 34 esto lee registros persistidos
    (DiscrepanciaInventario) en vez de recalcular la resta contra el
    stock actual. El cambio es el punto entero del rediseño: antes la
    alerta era literalmente `if diferencia != 0`, así que una venta
    posterior que hiciera cuadrar los números la borraba de la pantalla
    sin que nadie la hubiera revisado — el faltante real seguía ahí, ya
    sin ninguna señal. Ahora una alerta solo desaparece porque una
    persona la resolvió o la descartó.

    Los parámetros `motor` y `conteos_por_producto` se conservan por
    compatibilidad con home(), que los venía pasando para no repetir
    consultas; ya no se usan, porque leer discrepancias es una sola
    consulta y no hace falta calcular stock para nada.

    Devuelve una lista de dicts: {"producto", "conteo", "discrepancia",
    "diferencia", "total_pendientes", "requiere_revision"}, ordenada por
    el momento real del conteo pendiente más antiguo de cada producto.
    """
    from .models import DiscrepanciaInventario

    pendientes = (
        DiscrepanciaInventario.objects
        .filter(estado=DiscrepanciaInventario.PENDIENTE)
        .select_related("conteo", "producto")
        .order_by("conteo__fecha", "conteo__ocurrido_en")
    )

    por_producto = {}
    for discrepancia in pendientes:
        por_producto.setdefault(discrepancia.producto_id, []).append(discrepancia)

    alertas = []
    for lista in por_producto.values():
        primera = lista[0]
        alertas.append(
            {
                "producto": primera.producto,
                "conteo": primera.conteo,
                "discrepancia": primera,
                "diferencia": primera.diferencia_vigente,
                "total_pendientes": len(lista),
                "requiere_revision": any(d.requiere_revision for d in lista),
            }
        )

    alertas.sort(key=lambda a: (a["conteo"].fecha, a["conteo"].ocurrido_en))
    return alertas

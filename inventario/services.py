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
    ajustes_por_producto = {
        fila["producto_id"]: -(fila["total"] or 0)
        for fila in MovimientoSalida.objects.filter(
            producto_id__in=producto_ids, fecha__gte=fecha_inicio, fecha__lte=fecha_fin, tipo="ajuste"
        )
        .values("producto_id")
        .annotate(total=Sum("cantidad"))
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
        ganancia_neta = _money(ganancia_bruta - perdida_por_merma)

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
                "tipo_registro": "LoteCompra",
                "registro_id": lote.pk,
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
                "tipo_registro": "MovimientoSalida",
                "registro_id": salida.pk,
            }
        )

    conteos = list(conteos_qs)
    if conteos:
        # MotorStockCosto se construye UNA vez (3 consultas fijas) y
        # resuelve conteo.diferencia para cada conteo en memoria — antes,
        # la property ConteoFisico.diferencia disparaba su propio
        # stock_teorico(hasta_fecha=...) por cada conteo mostrado (33
        # consultas con 5 conteos, medido en el prompt 20; crecía 1:1 con
        # la cantidad de conteos en el rango). El resultado es idéntico a
        # conteo.diferencia (mismo MotorStockCosto que resumen_general(),
        # ver ParidadResumenGeneralTests) — solo cambia cómo se calcula.
        motor = MotorStockCosto()
        for conteo in conteos:
            diferencia = conteo.cantidad_contada - motor.stock_teorico(conteo.producto_id, hasta_fecha=conteo.fecha)
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
                    "tipo_registro": "ConteoFisico",
                    "registro_id": conteo.pk,
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

    Rendimiento (prompt 18b): antes recorría cada producto activo y por
    cada uno consultaba sus conteos_fisicos más la property .diferencia
    de cada conteo (consultas proporcionales a productos y conteos).
    Ahora usa un MotorStockCosto compartido (3 consultas fijas) y una
    única consulta de conteos — o la que ya trae el llamador via
    `conteos_por_producto`/`motor`, para no repetirla (ver home()).
    """
    motor = motor or MotorStockCosto()
    if conteos_por_producto is None:
        conteos_por_producto = conteos_activos_por_producto()

    alertas = []
    for producto_id, conteos in conteos_por_producto.items():
        pendientes = []
        for conteo in conteos:
            if conteo.ajuste_generado_id is not None:
                continue
            diferencia = conteo.cantidad_contada - motor.stock_teorico(producto_id, hasta_fecha=conteo.fecha)
            if diferencia != 0:
                pendientes.append((conteo, diferencia))
        if not pendientes:
            continue
        conteo_mas_antiguo, diferencia_mas_antigua = pendientes[0]
        alertas.append(
            {
                "producto": conteo_mas_antiguo.producto,
                "conteo": conteo_mas_antiguo,
                "diferencia": diferencia_mas_antigua,
                "total_pendientes": len(pendientes),
            }
        )

    alertas.sort(key=lambda a: a["conteo"].fecha)
    return alertas

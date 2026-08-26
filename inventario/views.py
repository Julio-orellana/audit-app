from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from .forms import (
    CategoriaForm,
    ConteoFisicoCorreccionForm,
    ConteoFisicoForm,
    HistorialFiltroForm,
    LoteCompraCorreccionForm,
    LoteCompraForm,
    MotivoCorreccionForm,
    MovimientoSalidaCorreccionForm,
    MovimientoSalidaForm,
    ProductoForm,
    ReporteForm,
)
from .antiduplicado import ProteccionDobleSubmitMixin
from .models import Categoria, ConteoFisico, CorreccionHistorial, LoteCompra, MovimientoSalida, Producto
from .permisos import RequiereRol, requiere_rol, rol_de
from .reportes import generar_reporte_excel
from .resiliencia import ManejoErrorConexionMixin, ReintentoEscrituraMixin, manejar_error_conexion
from .services import (
    MotorStockCosto,
    alertas_conteo_fisico,
    conteos_activos_por_producto,
    movimientos_periodo,
    productos_activos_por_categoria,
    resumen_general,
    serie_diaria_ventas,
    snapshot_registro,
)


def _saludo_segun_hora():
    hora = timezone.localtime().hour
    if hora < 12:
        return "Buenos días"
    if hora < 19:
        return "Buenas tardes"
    return "Buenas noches"


@login_required
@manejar_error_conexion
def home(request):
    if rol_de(request.user) == "vendedor":
        return _home_vendedor(request)

    productos = list(Producto.objects.filter(activo=True).select_related("categoria"))

    # Consultas fijas (prompt 18b): un MotorStockCosto (3 consultas) y una
    # sola consulta de conteos, compartidos entre el cálculo de stock/costo,
    # las alertas y "último conteo" de cada producto — antes esto disparaba
    # una consulta por producto (401 en total, medido en el prompt 20).
    motor = MotorStockCosto()
    conteos_por_producto = conteos_activos_por_producto()

    alertas = alertas_conteo_fisico(motor=motor, conteos_por_producto=conteos_por_producto)
    alerta_por_producto_id = {alerta["producto"].pk: alerta for alerta in alertas}

    filas_productos = []
    for producto in productos:
        conteos_del_producto = conteos_por_producto.get(producto.pk, [])
        ultimo_conteo = conteos_del_producto[-1] if conteos_del_producto else None
        filas_productos.append(
            {
                "producto": producto,
                "stock_teorico": motor.stock_teorico(producto.pk),
                "costo_promedio": motor.costo_promedio(producto.pk),
                "ultimo_conteo": ultimo_conteo,
                "alerta": alerta_por_producto_id.get(producto.pk),
            }
        )

    # Los productos con alerta activa van primero (sin importar categoría),
    # para que el auditor los vea de inmediato; el resto sigue agrupado por
    # categoría como antes. En cuanto se resuelve la alerta (se genera el
    # ajuste), el producto deja de estar en "filas_alerta" en el siguiente
    # render y vuelve a aparecer solo en su categoría normal.
    filas_alerta = [f for f in filas_productos if f["alerta"]]
    filas_normales = [f for f in filas_productos if not f["alerta"]]
    filas_alerta.sort(key=lambda f: f["alerta"]["conteo"].fecha)

    hoy = timezone.localdate()
    totales_hoy = resumen_general(hoy, hoy)["totales"]

    context = {
        "filas_alerta": filas_alerta,
        "filas_normales": filas_normales,
        "num_alertas": len(alertas),
        "unidades_vendidas_hoy": totales_hoy["unidades_vendidas"],
        "ganancia_neta_hoy": totales_hoy["ganancia_neta"],
        "hoy": hoy,
        "saludo": _saludo_segun_hora(),
    }
    return render(request, "inventario/home.html", context)


def _home_vendedor(request):
    """
    Dashboard reducido para el rol vendedor: sin ninguna cifra financiera
    (ni las tarjetas de resumen de hoy), solo el catálogo (nombre + precio,
    agrupado por categoría) y el acceso a registrar una venta.
    """
    context = {
        "hoy": timezone.localdate(),
        "saludo": _saludo_segun_hora(),
        "productos_por_categoria": productos_activos_por_categoria(),
    }
    return render(request, "inventario/home_vendedor.html", context)


# --- Categoria -------------------------------------------------------------
# Categorías y productos son catálogo/administración: admin y auditor
# tienen los mismos privilegios aquí (ver reglas de rol del prompt 16),
# vendedor no tiene acceso — su propio catálogo reducido vive en
# home_vendedor.html.

class CategoriaListView(RequiereRol("admin", "auditor"), generic.ListView):
    model = Categoria
    template_name = "inventario/categoria_list.html"
    context_object_name = "categorias"


class CategoriaCreateView(RequiereRol("admin", "auditor"), generic.CreateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


class CategoriaUpdateView(RequiereRol("admin", "auditor"), generic.UpdateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


@requiere_rol("admin", "auditor")
@require_POST
def categoria_toggle_activo(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    categoria.activo = not categoria.activo
    categoria.save()
    return redirect("categoria_list")


# --- Producto ----------------------------------------------------------------

class ProductoListView(RequiereRol("admin", "auditor"), generic.ListView):
    model = Producto
    template_name = "inventario/producto_list.html"
    context_object_name = "productos"
    queryset = Producto.objects.select_related("categoria", "producto_base")


class ProductoCreateView(RequiereRol("admin", "auditor"), generic.CreateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


class ProductoUpdateView(RequiereRol("admin", "auditor"), generic.UpdateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


@requiere_rol("admin", "auditor")
@require_POST
def producto_toggle_activo(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    producto.activo = not producto.activo
    producto.save()
    return redirect("producto_list")


# --- LoteCompra --------------------------------------------------------------

class LoteCompraCreateView(ProteccionDobleSubmitMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor"), generic.CreateView):
    model = LoteCompra
    form_class = LoteCompraForm
    template_name = "inventario/lotecompra_form.html"
    success_url = reverse_lazy("historial")

    def form_valid(self, form):
        form.instance.registrado_por = self.request.user
        return super().form_valid(form)


# --- MovimientoSalida ----------------------------------------------------------

class MovimientoSalidaCreateView(ProteccionDobleSubmitMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor", "vendedor"), generic.CreateView):
    """
    admin/auditor pueden registrar los 3 tipos (venta/merma/ajuste). Un
    vendedor solo puede registrar ventas — MovimientoSalidaForm recibe
    permitir_todos_los_tipos=False, que restringe las opciones mostradas Y
    rechaza en el backend cualquier tipo distinto de "venta" así se
    manipule el HTML o se mande el campo a mano (ver MovimientoSalidaForm).
    """
    model = MovimientoSalida
    form_class = MovimientoSalidaForm
    template_name = "inventario/movimientosalida_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["permitir_todos_los_tipos"] = rol_de(self.request.user) != "vendedor"
        return kwargs

    def get_success_url(self):
        # Un vendedor no tiene acceso a Historial — redirigirlo ahí sería
        # mandarlo directo a una pantalla que le da 403.
        if rol_de(self.request.user) == "vendedor":
            return reverse("home")
        return reverse("historial")

    def redirigir_tras_duplicado(self):
        # get_success_url() aqui no depende de self.object, es seguro
        # llamarlo aunque no se haya llegado a crear nada en este request.
        from django.shortcuts import redirect
        return redirect(self.get_success_url())

    def form_valid(self, form):
        form.instance.registrado_por = self.request.user
        return super().form_valid(form)


# --- ConteoFisico --------------------------------------------------------------

class ConteoFisicoCreateView(ProteccionDobleSubmitMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor"), generic.CreateView):
    model = ConteoFisico
    form_class = ConteoFisicoForm
    template_name = "inventario/conteofisico_form.html"
    # Al dashboard, no a la página de revisión del conteo: el auditor debe
    # decidir por su cuenta, uno por uno, qué alertas revisar — no se le
    # empuja automáticamente a resolver la que acaba de registrar.
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        form.instance.registrado_por = self.request.user
        return super().form_valid(form)


class ConteoFisicoDetailView(RequiereRol("admin", "auditor"), generic.DetailView):
    model = ConteoFisico
    template_name = "inventario/conteofisico_detail.html"
    context_object_name = "conteo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stock_teorico"] = self.object.producto.stock_teorico(hasta_fecha=self.object.fecha)
        return context


@requiere_rol("admin", "auditor")
@require_POST
def generar_ajuste(request, pk):
    # Condición de carrera (prompt 21, hallazgo 4.1 del diagnóstico): dos
    # POST casi simultáneos aquí podían leer ambos ajuste_generado_id como
    # None antes de que cualquiera terminara de guardar, y los dos creaban
    # su propio MovimientoSalida — el ajuste quedaba aplicado dos veces
    # sobre el stock real. select_for_update() dentro de transaction.atomic()
    # bloquea la fila del conteo: el segundo request que llega casi al
    # mismo tiempo espera a que el primero termine su transacción, y
    # cuando por fin lee el conteo ya ve ajuste_generado seteado — nunca
    # crea un segundo ajuste. (Ver también el unique=True en
    # ConteoFisico.ajuste_generado, capa adicional a nivel de base de
    # datos: nunca deja que dos ConteoFisico distintos terminen apuntando
    # al mismo MovimientoSalida, sin importar por dónde se haya escrito.)
    with transaction.atomic():
        conteo = get_object_or_404(ConteoFisico.objects.select_for_update(), pk=pk)

        if conteo.ajuste_generado_id:
            messages.info(request, "Este conteo ya tiene un ajuste generado.")
            return redirect("conteofisico_detail", pk=conteo.pk)

        diferencia = conteo.diferencia
        if diferencia == 0:
            messages.info(request, "No hay diferencia que ajustar.")
            return redirect("conteofisico_detail", pk=conteo.pk)

        ajuste = MovimientoSalida.objects.create(
            producto=conteo.producto,
            fecha=conteo.fecha,
            tipo="ajuste",
            # -diferencia: si faltó (diferencia<0) resta stock; si sobró
            # (diferencia>0) suma stock. Ver help_text de MovimientoSalida.cantidad.
            cantidad=-diferencia,
            motivo=f"Ajuste generado desde conteo físico #{conteo.pk} (diferencia {diferencia:+d})",
            registrado_por=request.user,
        )
        conteo.ajuste_generado = ajuste
        conteo.save()
    messages.success(request, "Ajuste generado correctamente.")
    return redirect("conteofisico_detail", pk=conteo.pk)


# --- Historial -----------------------------------------------------------------
# Nota para el prompt 17 (editar/eliminar historial): esas vistas deben
# usar RequiereRol("admin") — exclusivo de administrador, ni el auditor
# puede editar o borrar lo ya registrado (ver reglas de rol del prompt 16).

class HistorialView(ManejoErrorConexionMixin, RequiereRol("admin", "auditor"), generic.TemplateView):
    template_name = "inventario/historial.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = HistorialFiltroForm(self.request.GET or None)
        context["form"] = form

        productos = None
        fecha_desde = None
        fecha_hasta = None
        if form.is_valid():
            producto = form.cleaned_data.get("producto")
            fecha_desde = form.cleaned_data.get("fecha_desde")
            fecha_hasta = form.cleaned_data.get("fecha_hasta")
            if producto:
                productos = [producto]

        # Sin filtro de fecha, se muestra todo el historial: un rango muy
        # amplio hace las veces de "sin límite" para movimientos_periodo().
        fecha_desde = fecha_desde or date(1900, 1, 1)
        fecha_hasta = fecha_hasta or date(2999, 12, 31)

        context["filas"] = movimientos_periodo(fecha_desde, fecha_hasta, productos=productos)
        return context


# --- Reportes --------------------------------------------------------------

class ReporteView(ManejoErrorConexionMixin, RequiereRol("admin", "auditor"), generic.View):
    """
    GET sin periodo/rango: muestra el formulario de selección (periodo +
    productos).
    GET con ?periodo=hoy|semana|mes, o con fecha_inicio/fecha_fin válidos:
    muestra estadísticas y gráficas en pantalla para ese rango + productos
    seleccionados (sin descargar nada todavía).
    Mismo GET + &descargar=1: genera y descarga el Excel (2 hojas:
    Resumen y Movimientos; las gráficas se consultan en pantalla).

    Selección de productos: vacío = todos los productos activos: una
    lista de ids en "productos" = solo esos (ver ReporteForm).
    """

    template_name = "inventario/reportes_form.html"

    def get(self, request, *args, **kwargs):
        form = ReporteForm(request.GET or None)
        periodo = request.GET.get("periodo")

        # Acceder a form.errors ya dispara la validación completa (si el
        # form está bound) — así form.cleaned_data queda disponible más
        # abajo para leer fecha_inicio/fecha_fin de forma independiente
        # de si "productos" en particular falló su propia validación
        # (prompt 22, punto 4.8): un producto inactivo forzado por URL no
        # debe tumbar también un reporte de rango de fechas por lo demás
        # válido, y el usuario debe enterarse de que su selección se
        # ignoró, en vez de que se sustituya en silencio.
        if form.errors.get("productos"):
            messages.warning(
                request,
                "Uno o más productos seleccionados ya no están activos y fueron excluidos del reporte.",
            )
        cleaned = getattr(form, "cleaned_data", {})

        fecha_inicio = fecha_fin = None
        if periodo in ("hoy", "semana", "mes"):
            fecha_inicio, fecha_fin = self._rango_desde_periodo(periodo)
        elif cleaned.get("fecha_inicio") and cleaned.get("fecha_fin"):
            fecha_inicio = cleaned["fecha_inicio"]
            fecha_fin = cleaned["fecha_fin"]

        productos_por_categoria = productos_activos_por_categoria()
        # Los checkboxes deben reflejar lo que el usuario marcó incluso si
        # todavía no hay periodo — si no, se pierden en el viaje de ida y
        # vuelta cuando le avisamos que falta elegir un periodo.
        productos_marcados = cleaned.get("productos")
        productos_seleccionados_ids = {p.pk for p in productos_marcados} if productos_marcados else set()

        if fecha_inicio is None or fecha_fin is None:
            # "intento" viaja en cada submit del formulario (ver template):
            # si está presente mostrar un mensaje claro en vez de fallar en
            # silencio; en la primera carga de la página (sin "intento") no
            # hay nada que avisar todavía.
            if "intento" in request.GET:
                messages.warning(
                    request,
                    "Selecciona un periodo (Hoy, Esta semana, Este mes, o un rango personalizado) para generar el reporte.",
                )
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "productos_por_categoria": productos_por_categoria,
                    "productos_seleccionados_ids": productos_seleccionados_ids,
                },
            )

        productos = self._resolver_productos(form)

        if request.GET.get("descargar") == "1":
            return self._descargar(fecha_inicio, fecha_fin, productos)

        contexto = self._contexto_estadisticas(fecha_inicio, fecha_fin, productos)
        contexto.update(
            {
                "form": form,
                "productos_por_categoria": productos_por_categoria,
                "productos_seleccionados_ids": productos_seleccionados_ids,
            }
        )
        return render(request, self.template_name, contexto)

    @staticmethod
    def _rango_desde_periodo(periodo):
        hoy = timezone.localdate()
        if periodo == "hoy":
            return hoy, hoy
        if periodo == "semana":
            inicio = hoy - timedelta(days=hoy.weekday())  # lunes de esta semana
            return inicio, hoy
        if periodo == "mes":
            return hoy.replace(day=1), hoy
        return hoy, hoy

    @staticmethod
    def _resolver_productos(form):
        """None = todos los productos activos; lista = solo los elegidos."""
        if form.is_valid():
            seleccionados = form.cleaned_data.get("productos")
            if seleccionados:
                return list(seleccionados)
        return None

    def _contexto_estadisticas(self, fecha_inicio, fecha_fin, productos):
        resumen = resumen_general(fecha_inicio, fecha_fin, productos=productos)

        # Barras: hasta 8 productos con mayor ganancia neta (en valor
        # absoluto, para que una pérdida grande no quede oculta), para que
        # el gráfico se mantenga legible aunque se incluyan muchos
        # productos. Verde = ganancia, rojo = pérdida — el auditor debe
        # poder leer el resultado con solo mirar el color y la etiqueta,
        # sin ir a la tabla.
        barras = sorted(resumen["productos"], key=lambda f: abs(f["ganancia_neta"]), reverse=True)[:8]
        max_abs = max((abs(f["ganancia_neta"]) for f in barras), default=Decimal("0.00"))
        for fila in barras:
            valor = fila["ganancia_neta"]
            fila["es_negativa"] = valor < 0
            if max_abs > 0:
                fila["barra_pct"] = max(float(abs(valor) / max_abs * 100), 3)
            else:
                fila["barra_pct"] = 0

        # Línea de tendencia de ventas: solo si el rango cubre más de un
        # día (igual criterio que antes usaba el Excel).
        puntos_linea = ""
        area_linea = ""
        etiqueta_primer_dia = etiqueta_ultimo_dia = ""
        pico_x = pico_y = pico_valor = None
        if fecha_fin > fecha_inicio:
            serie = serie_diaria_ventas(fecha_inicio, fecha_fin, productos=productos)
            ancho, alto = 440, 160
            # Margen superior reservado para que la etiqueta del valor pico
            # nunca quede cortada contra el borde del gráfico (sin esto, el
            # punto más alto caía en y=0 y la etiqueta se salía del SVG).
            margen_superior = 24
            alto_dibujo = alto - margen_superior
            n = len(serie)
            max_val = max((valor for _, valor in serie), default=Decimal("0"))
            if max_val <= 0:
                max_val = Decimal("1")
            coords = []
            for i, (_fecha, valor) in enumerate(serie):
                x = round(i / (n - 1) * ancho, 1) if n > 1 else 0
                y = round(margen_superior + alto_dibujo - (float(valor) / float(max_val) * alto_dibujo), 1)
                coords.append((x, y))
            if coords:
                puntos_linea = " ".join(f"{x},{y}" for x, y in coords)
                area_linea = puntos_linea + f" {coords[-1][0]},{alto} {coords[0][0]},{alto}"
                etiqueta_primer_dia = serie[0][0]
                etiqueta_ultimo_dia = serie[-1][0]

                valores_serie = [valor for _, valor in serie]
                idx_pico = max(range(len(valores_serie)), key=lambda i: valores_serie[i])
                if valores_serie[idx_pico] > 0:
                    # Gracias al margen_superior reservado arriba, pico_y
                    # nunca baja de ese margen — la etiqueta siempre cabe.
                    pico_x, pico_y = coords[idx_pico]
                    pico_valor = valores_serie[idx_pico]

        num_productos_incluidos = len(productos) if productos is not None else Producto.objects.filter(activo=True).count()

        params = {
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_fin": fecha_fin.isoformat(),
            "descargar": "1",
        }
        pares = list(params.items())
        if productos is not None:
            pares += [("productos", p.pk) for p in productos]
        descarga_url = f"{reverse('reportes')}?{urlencode(pares)}"

        return {
            "resumen": resumen,
            "barras": barras,
            "puntos_linea": puntos_linea,
            "area_linea": area_linea,
            "etiqueta_primer_dia": etiqueta_primer_dia,
            "etiqueta_ultimo_dia": etiqueta_ultimo_dia,
            "pico_x": pico_x,
            "pico_y": pico_y,
            "pico_valor": pico_valor,
            "num_productos_incluidos": num_productos_incluidos,
            "fecha_inicio": fecha_inicio,
            "fecha_fin": fecha_fin,
            "descarga_url": descarga_url,
        }

    @staticmethod
    def _descargar(fecha_inicio, fecha_fin, productos):
        buffer = generar_reporte_excel(fecha_inicio, fecha_fin, productos=productos)
        nombre_archivo = f"reporte_auditoria_{fecha_inicio}_{fecha_fin}.xlsx"
        response = HttpResponse(
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{nombre_archivo}"'
        return response


# --- Instrucciones -----------------------------------------------------------

class InstruccionesView(LoginRequiredMixin, generic.TemplateView):
    template_name = "inventario/instrucciones.html"


# --- Correcciones al historial (prompt 17, exclusivo de admin) ---------------
#
# Editar o eliminar un LoteCompra/MovimientoSalida/ConteoFisico ya guardado
# cambia conscientemente la regla original de que un reporte de un mes
# cerrado nunca cambia (ver docstring de CorreccionHistorial en models.py).
# A cambio, cada corrección exige un motivo y queda registrada con su
# snapshot antes/después en la MISMA transacción que el cambio real —
# nunca puede quedar uno sin el otro.

class CorreccionUpdateView(RequiereRol("admin"), generic.UpdateView):
    """Base compartida para editar un registro ya guardado. Cada subclase
    solo define model/form_class/titulo_registro."""
    template_name = "inventario/correccion_form.html"
    success_url = reverse_lazy("historial")
    titulo_registro = "registro"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["titulo_registro"] = self.titulo_registro
        return context

    def post(self, request, *args, **kwargs):
        # Se captura ANTES de que el form (ya con los datos del POST)
        # mute self.object en memoria durante is_valid() — por eso no se
        # puede snapshotear en form_valid(), ya sería tarde.
        self.object = self.get_object()
        self._snapshot_antes = snapshot_registro(self.object)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        motivo = form.cleaned_data["motivo_correccion"]
        tipo_registro = type(self.object).__name__
        with transaction.atomic():
            response = super().form_valid(form)
            CorreccionHistorial.objects.create(
                tipo_registro=tipo_registro,
                registro_id=self.object.pk,
                accion="edicion",
                datos_anteriores=self._snapshot_antes,
                datos_nuevos=snapshot_registro(self.object),
                motivo=motivo,
                realizado_por=self.request.user,
            )
        messages.success(self.request, "Corrección guardada — queda registrada en Correcciones al historial.")
        return response


class LoteCompraCorreccionUpdateView(CorreccionUpdateView):
    model = LoteCompra
    form_class = LoteCompraCorreccionForm
    titulo_registro = "entrada de compra"


class MovimientoSalidaCorreccionUpdateView(CorreccionUpdateView):
    model = MovimientoSalida
    form_class = MovimientoSalidaCorreccionForm
    titulo_registro = "salida"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        conteos = ConteoFisico.objects.filter(ajuste_generado=self.object)
        if conteos.exists():
            lista = ", ".join(f"#{c.pk}" for c in conteos)
            context["advertencia"] = (
                f"Este movimiento es el ajuste generado del conteo físico {lista}. Cambiar su "
                f"cantidad aquí no actualiza la diferencia ya calculada de ese conteo — a partir de "
                f"ahora quedarían desalineados si no corriges también el conteo."
            )
        return context


class ConteoFisicoCorreccionUpdateView(CorreccionUpdateView):
    model = ConteoFisico
    form_class = ConteoFisicoCorreccionForm
    titulo_registro = "conteo físico"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.object.ajuste_generado_id:
            context["advertencia"] = (
                f"Este conteo ya generó el ajuste MovimientoSalida #{self.object.ajuste_generado_id}. "
                f"Editar la cantidad contada aquí NO ajusta automáticamente ese movimiento ya "
                f"generado — corrígelo también por separado si la diferencia cambia."
            )
        return context


class CorreccionDeleteView(RequiereRol("admin"), generic.DeleteView):
    """
    Base compartida para eliminar un registro ya guardado. No usa el flujo
    de confirmación por defecto de DeleteView (un solo POST sin más) —
    exige motivo (MotivoCorreccionForm) y dos ganchos que cada subclase
    puede usar para la relación ConteoFisico.ajuste_generado (prompt 17,
    punto 5): _bloqueo_eliminacion() para impedirla del todo, o
    _advertencia_eliminacion() para avisar sin impedirla.
    """
    form_class = MotivoCorreccionForm
    template_name = "inventario/correccion_confirm_delete.html"
    titulo_registro = "registro"

    def _bloqueo_eliminacion(self):
        return None

    def _advertencia_eliminacion(self):
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", self.form_class())
        context["titulo_registro"] = self.titulo_registro
        context["bloqueo"] = self._bloqueo_eliminacion()
        context["advertencia"] = self._advertencia_eliminacion()
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        bloqueo = self._bloqueo_eliminacion()
        if bloqueo:
            messages.error(request, bloqueo)
            return redirect(self.get_success_url())

        form = self.form_class(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        motivo = form.cleaned_data["motivo_correccion"]
        snapshot_antes = snapshot_registro(self.object)
        registro_id = self.object.pk
        tipo_registro = type(self.object).__name__

        with transaction.atomic():
            self.object.delete()
            CorreccionHistorial.objects.create(
                tipo_registro=tipo_registro,
                registro_id=registro_id,
                accion="eliminacion",
                datos_anteriores=snapshot_antes,
                datos_nuevos=None,
                motivo=motivo,
                realizado_por=request.user,
            )
        messages.success(request, "Registro eliminado — queda registrada la corrección en Correcciones al historial.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("historial")


class LoteCompraCorreccionDeleteView(CorreccionDeleteView):
    model = LoteCompra
    titulo_registro = "entrada de compra"


class MovimientoSalidaCorreccionDeleteView(CorreccionDeleteView):
    model = MovimientoSalida
    titulo_registro = "salida"

    def _bloqueo_eliminacion(self):
        conteos = ConteoFisico.objects.filter(ajuste_generado=self.object)
        if conteos.exists():
            lista = ", ".join(f"#{c.pk}" for c in conteos)
            return (
                f"No se puede eliminar: este movimiento es el ajuste generado del conteo físico "
                f"{lista}. Corrige o elimina primero ese conteo si de verdad necesitas deshacer la "
                f"relación — eliminarlo aquí directamente dejaría ese conteo con una referencia rota."
            )
        return None


class ConteoFisicoCorreccionDeleteView(CorreccionDeleteView):
    model = ConteoFisico
    titulo_registro = "conteo físico"

    def _advertencia_eliminacion(self):
        if self.object.ajuste_generado_id:
            return (
                f"Este conteo ya generó el ajuste MovimientoSalida #{self.object.ajuste_generado_id}. "
                f"Al eliminar este conteo, ese movimiento seguirá existiendo en el historial de forma "
                f"independiente, sin conteo de origen."
            )
        return None


class CorreccionHistorialListView(RequiereRol("admin"), generic.ListView):
    model = CorreccionHistorial
    template_name = "inventario/correccion_historial_list.html"
    context_object_name = "correcciones"

    def get_queryset(self):
        return CorreccionHistorial.objects.select_related("realizado_por").order_by("-fecha")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filas = []
        for correccion in context["correcciones"]:
            cambios = None
            if correccion.accion == "edicion" and correccion.datos_nuevos is not None:
                cambios = [
                    (campo, valor_antes, correccion.datos_nuevos.get(campo))
                    for campo, valor_antes in correccion.datos_anteriores.items()
                    if valor_antes != correccion.datos_nuevos.get(campo)
                ]
            filas.append({"correccion": correccion, "cambios": cambios})
        context["filas"] = filas
        return context

from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import generic
from django.views.decorators.http import require_POST

from .forms import (
    CategoriaForm,
    ConteoFisicoForm,
    HistorialFiltroForm,
    LoteCompraForm,
    MovimientoSalidaForm,
    ProductoForm,
    ReporteForm,
)
from .models import Categoria, ConteoFisico, LoteCompra, MovimientoSalida, Producto
from .reportes import generar_reporte_excel
from .services import alertas_conteo_fisico, movimientos_periodo, resumen_general, serie_diaria_ventas


@login_required
def home(request):
    productos = Producto.objects.filter(activo=True).select_related("categoria")

    alertas = alertas_conteo_fisico()
    alerta_por_producto_id = {alerta["producto"].pk: alerta for alerta in alertas}

    filas_productos = []
    for producto in productos:
        ultimo_conteo = producto.conteos_fisicos.order_by("-fecha", "-creado_en").first()
        filas_productos.append(
            {
                "producto": producto,
                "stock_teorico": producto.stock_teorico(),
                "costo_promedio": producto.costo_promedio(),
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

    hora = timezone.localtime().hour
    if hora < 12:
        saludo = "Buenos días"
    elif hora < 19:
        saludo = "Buenas tardes"
    else:
        saludo = "Buenas noches"

    context = {
        "filas_alerta": filas_alerta,
        "filas_normales": filas_normales,
        "num_alertas": len(alertas),
        "unidades_vendidas_hoy": totales_hoy["unidades_vendidas"],
        "ganancia_neta_hoy": totales_hoy["ganancia_neta"],
        "hoy": hoy,
        "saludo": saludo,
    }
    return render(request, "inventario/home.html", context)


# --- Categoria -------------------------------------------------------------

class CategoriaListView(LoginRequiredMixin, generic.ListView):
    model = Categoria
    template_name = "inventario/categoria_list.html"
    context_object_name = "categorias"


class CategoriaCreateView(LoginRequiredMixin, generic.CreateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


class CategoriaUpdateView(LoginRequiredMixin, generic.UpdateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


@login_required
@require_POST
def categoria_toggle_activo(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    categoria.activo = not categoria.activo
    categoria.save()
    return redirect("categoria_list")


# --- Producto ----------------------------------------------------------------

class ProductoListView(LoginRequiredMixin, generic.ListView):
    model = Producto
    template_name = "inventario/producto_list.html"
    context_object_name = "productos"
    queryset = Producto.objects.select_related("categoria", "producto_base")


class ProductoCreateView(LoginRequiredMixin, generic.CreateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


class ProductoUpdateView(LoginRequiredMixin, generic.UpdateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


@login_required
@require_POST
def producto_toggle_activo(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    producto.activo = not producto.activo
    producto.save()
    return redirect("producto_list")


# --- LoteCompra --------------------------------------------------------------

class LoteCompraCreateView(LoginRequiredMixin, generic.CreateView):
    model = LoteCompra
    form_class = LoteCompraForm
    template_name = "inventario/lotecompra_form.html"
    success_url = reverse_lazy("historial")

    def form_valid(self, form):
        form.instance.registrado_por = self.request.user
        return super().form_valid(form)


# --- MovimientoSalida ----------------------------------------------------------

class MovimientoSalidaCreateView(LoginRequiredMixin, generic.CreateView):
    model = MovimientoSalida
    form_class = MovimientoSalidaForm
    template_name = "inventario/movimientosalida_form.html"
    success_url = reverse_lazy("historial")

    def form_valid(self, form):
        form.instance.registrado_por = self.request.user
        return super().form_valid(form)


# --- ConteoFisico --------------------------------------------------------------

class ConteoFisicoCreateView(LoginRequiredMixin, generic.CreateView):
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


class ConteoFisicoDetailView(LoginRequiredMixin, generic.DetailView):
    model = ConteoFisico
    template_name = "inventario/conteofisico_detail.html"
    context_object_name = "conteo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stock_teorico"] = self.object.producto.stock_teorico(hasta_fecha=self.object.fecha)
        return context


@login_required
@require_POST
def generar_ajuste(request, pk):
    conteo = get_object_or_404(ConteoFisico, pk=pk)

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

class HistorialView(LoginRequiredMixin, generic.TemplateView):
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

class ReporteView(LoginRequiredMixin, generic.View):
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

        fecha_inicio = fecha_fin = None
        if periodo in ("hoy", "semana", "mes"):
            fecha_inicio, fecha_fin = self._rango_desde_periodo(periodo)
        elif form.is_valid() and form.cleaned_data.get("fecha_inicio") and form.cleaned_data.get("fecha_fin"):
            fecha_inicio = form.cleaned_data["fecha_inicio"]
            fecha_fin = form.cleaned_data["fecha_fin"]

        productos_por_categoria = self._productos_por_categoria()
        # Los checkboxes deben reflejar lo que el usuario marcó incluso si
        # todavía no hay periodo — si no, se pierden en el viaje de ida y
        # vuelta cuando le avisamos que falta elegir un periodo.
        productos_marcados = form.cleaned_data.get("productos") if form.is_valid() else None
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
    def _productos_por_categoria():
        productos = (
            Producto.objects.filter(activo=True)
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
            n = len(serie)
            max_val = max((valor for _, valor in serie), default=Decimal("0"))
            if max_val <= 0:
                max_val = Decimal("1")
            coords = []
            for i, (_fecha, valor) in enumerate(serie):
                x = round(i / (n - 1) * ancho, 1) if n > 1 else 0
                y = round(alto - (float(valor) / float(max_val) * alto), 1)
                coords.append((x, y))
            if coords:
                puntos_linea = " ".join(f"{x},{y}" for x, y in coords)
                area_linea = puntos_linea + f" {coords[-1][0]},{alto} {coords[0][0]},{alto}"
                etiqueta_primer_dia = serie[0][0]
                etiqueta_ultimo_dia = serie[-1][0]

                valores_serie = [valor for _, valor in serie]
                idx_pico = max(range(len(valores_serie)), key=lambda i: valores_serie[i])
                if valores_serie[idx_pico] > 0:
                    pico_x, pico_y = coords[idx_pico]
                    pico_y = max(pico_y, 14)  # que la etiqueta no se salga arriba del gráfico
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

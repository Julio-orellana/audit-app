import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.views import LoginView, LogoutView
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
from .offline import (
    ALIAS_LOCAL,
    CLAVE_SESION_OFFLINE,
    ColaOfflineMixin,
    ERRORES_DE_CONEXION,
    completar_snapshot_offline,
    contar_pendientes,
    hay_conexion,
)
from .permisos import RequiereRol, requiere_rol, rol_de
from .reportes import generar_reporte_excel
from .resiliencia import (
    ManejoErrorConexionMixin,
    ReintentoEscrituraMixin,
    RequiereConexionMixin,
    funciona_sin_conexion,
    manejar_error_conexion,
    requiere_conexion,
)
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


class LoginConRespaldoOfflineView(LoginView):
    """
    Inicio de sesión que TAMBIÉN funciona sin conexión (prompt 19b,
    punto 1).

    La validación en sí la hace BackendConRespaldoOffline
    (inventario/offline.py): con internet valida contra Neon como
    siempre; sin internet valida contra la caché local de credenciales
    de esta máquina. Aquí solo queda resolver el último detalle: la
    señal user_logged_in de Django dispara update_last_login(), que
    escribe en auth_user — y eso, sin conexión, revienta DESPUÉS de que
    la sesión ya quedó correctamente establecida. Se atrapa ese caso
    concreto (verificando que la sesión sí quedó) en vez de dejar que
    tumbe un inicio de sesión que en realidad funcionó.
    """
    funciona_sin_conexion = True

    def form_valid(self, form):
        entro_sin_conexion = getattr(form.get_user(), "_resuelto_offline", False)
        try:
            respuesta = super().form_valid(form)
        except ERRORES_DE_CONEXION:
            if not self.request.session.get(SESSION_KEY):
                # La sesión NO llegó a establecerse: esto no es el caso
                # de last_login, es un fallo real — que lo tome el
                # middleware global y muestre la pantalla clara.
                raise
            respuesta = redirect(self.get_success_url())
        if entro_sin_conexion:
            self.request.session[CLAVE_SESION_OFFLINE] = True
            messages.info(
                self.request,
                "Entraste sin conexión a internet. Puedes registrar movimientos "
                "normalmente: se guardan en este equipo y se sincronizan solos "
                "en cuanto vuelva la conexión.",
            )
        return respuesta


class LogoutConRespaldoOfflineView(LogoutView):
    """
    Cerrar sesión no toca la base (la sesión vive en archivo, ver
    SESSION_ENGINE en settings.py), así que funciona igual sin conexión.
    La subclase existe solo para dejarlo clasificado explícitamente
    (prompt 19b, punto 4).
    """
    funciona_sin_conexion = True


@funciona_sin_conexion
@login_required
@manejar_error_conexion
def home(request):
    if not hay_conexion():
        # Dashboard básico offline (prompt 19): mismo catálogo cacheado
        # reducido (nombre + precio, sin cifras financieras — esas no se
        # pueden calcular sin conexión) para CUALQUIER rol, no solo
        # vendedor — admin/auditor pierden su dashboard completo mientras
        # dure el corte, pero siguen viendo el catálogo y pueden seguir
        # registrando movimientos (la cola offline los guarda igual).
        return _home_offline(request)
    if rol_de(request.user) == "vendedor":
        return _home_vendedor(request)

    # select_related("producto_base") (prompt 24): home.html muestra
    # "Producto base: X" bajo cada producto derivado — sin esto, acceder a
    # fila.producto.producto_base en el template dispara una consulta
    # perdida POR CADA producto derivado del catálogo (7 en el catálogo
    # actual), algo que el diagnóstico de rendimiento del prompt 18b no
    # detectó porque solo contaba consultas totales sin desglosar de
    # dónde salían — cada una de esas consultas paga la misma latencia de
    # red fija que cualquier otra contra Neon, así que 7 de más sí se
    # sienten en un navegador real aunque no sean muchas en total.
    productos = list(Producto.objects.filter(activo=True).select_related("categoria", "producto_base"))

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
    totales_hoy = resumen_general(hoy, hoy, motor=motor)["totales"]

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
        "pendientes": contar_pendientes(),
    }
    return render(request, "inventario/home_vendedor.html", context)


def _home_offline(request):
    """
    Mismo template reducido que _home_vendedor() (prompt 19) — el
    catálogo cacheado en "local_disco" es lo único que se puede mostrar
    con confianza sin conexión, sea cual sea el rol. "modo_offline" en el
    contexto agrega el aviso — nunca se pasa True en el uso normal, así
    que un vendedor con conexión ve exactamente lo mismo que siempre.
    """
    context = {
        "hoy": timezone.localdate(),
        "saludo": _saludo_segun_hora(),
        "productos_por_categoria": productos_activos_por_categoria(alias=ALIAS_LOCAL),
        "pendientes": contar_pendientes(),
        "modo_offline": True,
    }
    return render(request, "inventario/home_vendedor.html", context)


# --- Categoria -------------------------------------------------------------
# Categorías y productos son catálogo/administración: admin y auditor
# tienen los mismos privilegios aquí (ver reglas de rol del prompt 16),
# vendedor no tiene acceso — su propio catálogo reducido vive en
# home_vendedor.html.

class CategoriaListView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.ListView):
    model = Categoria
    template_name = "inventario/categoria_list.html"
    context_object_name = "categorias"


class CategoriaCreateView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.CreateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


class CategoriaUpdateView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.UpdateView):
    model = Categoria
    form_class = CategoriaForm
    template_name = "inventario/categoria_form.html"
    success_url = reverse_lazy("categoria_list")


@requiere_rol("admin", "auditor")
@requiere_conexion
@require_POST
def categoria_toggle_activo(request, pk):
    categoria = get_object_or_404(Categoria, pk=pk)
    categoria.activo = not categoria.activo
    categoria.save()
    return redirect("categoria_list")


# --- Producto ----------------------------------------------------------------

class ProductoListView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.ListView):
    model = Producto
    template_name = "inventario/producto_list.html"
    context_object_name = "productos"
    queryset = Producto.objects.select_related("categoria", "producto_base")


class ProductoCreateView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.CreateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


class ProductoUpdateView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.UpdateView):
    model = Producto
    form_class = ProductoForm
    template_name = "inventario/producto_form.html"
    success_url = reverse_lazy("producto_list")


@requiere_rol("admin", "auditor")
@requiere_conexion
@require_POST
def producto_toggle_activo(request, pk):
    producto = get_object_or_404(Producto, pk=pk)
    producto.activo = not producto.activo
    producto.save()
    return redirect("producto_list")


# --- LoteCompra --------------------------------------------------------------

class LoteCompraCreateView(ProteccionDobleSubmitMixin, ColaOfflineMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor"), generic.CreateView):
    # Funciona sin conexión vía ColaOfflineMixin (prompt 19/19b): la
    # entrada se guarda en la cola local y se sincroniza sola.
    funciona_sin_conexion = True
    model = LoteCompra
    form_class = LoteCompraForm
    template_name = "inventario/lotecompra_form.html"
    success_url = reverse_lazy("historial")

    def form_valid(self, form):
        # .registrado_por_id (no .registrado_por = ...): en modo offline
        # (prompt 19) self.request.user puede ser un UsuarioOffline, no
        # una instancia real de auth.User — asignarlo directo al FK
        # revienta con ValueError. Asignar el id crudo funciona igual en
        # ambos casos (con conexión real o con el respaldo offline).
        form.instance.registrado_por_id = self.request.user.pk
        return super().form_valid(form)


# --- MovimientoSalida ----------------------------------------------------------

class MovimientoSalidaCreateView(ProteccionDobleSubmitMixin, ColaOfflineMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor", "vendedor"), generic.CreateView):
    """
    admin/auditor pueden registrar los 3 tipos (venta/merma/ajuste). Un
    vendedor solo puede registrar ventas — MovimientoSalidaForm recibe
    permitir_todos_los_tipos=False, que restringe las opciones mostradas Y
    rechaza en el backend cualquier tipo distinto de "venta" así se
    manipule el HTML o se mande el campo a mano (ver MovimientoSalidaForm).
    """
    # Funciona sin conexión vía ColaOfflineMixin (prompt 19/19b).
    funciona_sin_conexion = True
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
        # .registrado_por_id (no .registrado_por = ...): en modo offline
        # (prompt 19) self.request.user puede ser un UsuarioOffline, no
        # una instancia real de auth.User — asignarlo directo al FK
        # revienta con ValueError. Asignar el id crudo funciona igual en
        # ambos casos (con conexión real o con el respaldo offline).
        form.instance.registrado_por_id = self.request.user.pk
        if rol_de(self.request.user) == "vendedor":
            # Igual que el tipo (ver docstring de la clase): un vendedor
            # no puede elegir fecha, ni siquiera mandando un HTML
            # manipulado o una fecha distinta a mano en el POST — esto
            # ignora lo que haya llegado y siempre fuerza la de hoy
            # (prompt 29). El campo aparece oculto en el formulario, pero
            # esa es solo la capa visual; esta es la que de verdad cuenta.
            # Se calcula ANTES del chequeo de conexión de abajo: la fecha
            # de una venta offline sigue siendo la de HOY (momento de
            # creación local), nunca la del momento en que sincronice
            # (prompt 19, punto 7).
            form.instance.fecha = timezone.localdate()
        if not hay_conexion():
            # MovimientoSalida.save() normalmente calcula
            # costo_unitario_snapshot/precio_venta_unitario consultando la
            # base — sin conexión eso nunca se alcanza a ejecutar. Se
            # completa aquí desde la caché local del catálogo (prompt 19)
            # ANTES de que ColaOfflineMixin (más abajo en la cadena)
            # encole la instancia, para que quede con un snapshot
            # "congelado" al momento real de la venta.
            completar_snapshot_offline(form.instance)
        respuesta = super().form_valid(form)
        mensajes_por_tipo = {
            "venta": "Venta registrada correctamente.",
            "merma": "Merma registrada correctamente.",
            "ajuste": "Ajuste registrado correctamente.",
        }
        messages.success(self.request, mensajes_por_tipo.get(form.instance.tipo, "Salida registrada correctamente."))
        return respuesta

    def form_invalid(self, form):
        messages.error(self.request, "No se pudo registrar: revisa los datos marcados abajo.")
        return super().form_invalid(form)


# --- ConteoFisico --------------------------------------------------------------

class ConteoFisicoCreateView(ProteccionDobleSubmitMixin, ColaOfflineMixin, ReintentoEscrituraMixin, RequiereRol("admin", "auditor"), generic.CreateView):
    # Funciona sin conexión vía ColaOfflineMixin (prompt 19/19b).
    funciona_sin_conexion = True
    model = ConteoFisico
    form_class = ConteoFisicoForm
    template_name = "inventario/conteofisico_form.html"
    # Al dashboard, no a la página de revisión del conteo: el auditor debe
    # decidir por su cuenta, uno por uno, qué alertas revisar — no se le
    # empuja automáticamente a resolver la que acaba de registrar.
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        # .registrado_por_id (no .registrado_por = ...): en modo offline
        # (prompt 19) self.request.user puede ser un UsuarioOffline, no
        # una instancia real de auth.User — asignarlo directo al FK
        # revienta con ValueError. Asignar el id crudo funciona igual en
        # ambos casos (con conexión real o con el respaldo offline).
        form.instance.registrado_por_id = self.request.user.pk
        return super().form_valid(form)


class ConteoFisicoDetailView(RequiereRol("admin", "auditor"), RequiereConexionMixin, generic.DetailView):
    model = ConteoFisico
    template_name = "inventario/conteofisico_detail.html"
    context_object_name = "conteo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stock_teorico"] = self.object.producto.stock_teorico(hasta_fecha=self.object.fecha)
        return context


@requiere_rol("admin", "auditor")
@requiere_conexion
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

class HistorialView(RequiereRol("admin", "auditor"), RequiereConexionMixin, ManejoErrorConexionMixin, generic.TemplateView):
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

class ReporteView(RequiereRol("admin", "auditor"), RequiereConexionMixin, ManejoErrorConexionMixin, generic.View):
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
    # Contenido estático: no consulta la base, funciona sin conexión.
    funciona_sin_conexion = True
    template_name = "inventario/instrucciones.html"


# --- Correcciones al historial (prompt 17, exclusivo de admin) ---------------
#
# Editar o eliminar un LoteCompra/MovimientoSalida/ConteoFisico ya guardado
# cambia conscientemente la regla original de que un reporte de un mes
# cerrado nunca cambia (ver docstring de CorreccionHistorial en models.py).
# A cambio, cada corrección exige un motivo y queda registrada con su
# snapshot antes/después en la MISMA transacción que el cambio real —
# nunca puede quedar uno sin el otro.

class CorreccionUpdateView(RequiereRol("admin"), RequiereConexionMixin, ManejoErrorConexionMixin, generic.UpdateView):
    """
    Base compartida para editar un registro ya guardado. Cada subclase
    solo define model/form_class/titulo_registro.

    ManejoErrorConexionMixin (no ColaOfflineMixin): editar/eliminar
    historial NUNCA pasa por la cola offline, bajo ninguna circunstancia
    (decisión explícita del prompt 19 — un mensaje claro pidiendo
    conexión es lo único que corresponde aquí, la operación en sí se
    bloquea del todo).
    """
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
        # MovimientoSalidaCorreccionForm no tiene "motivo_correccion" —
        # reutiliza el campo "motivo" del modelo, ver su docstring en
        # forms.py.
        motivo = form.cleaned_data.get("motivo_correccion") or form.cleaned_data["motivo"]
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
    """
    precio_venta_unitario/costo_unitario_snapshot (prompt 28): al crear un
    MovimientoSalida, MovimientoSalida.save() los calcula del producto —
    pero solo "si están vacíos", pensado para la creación. Al EDITAR ya
    tienen un valor (el snapshot original), así que ese guard nunca
    recalculaba nada al cambiar el producto — el precio/costo del
    producto ANTERIOR quedaba pegado al nuevo, corrompiendo el reporte
    financiero de ese movimiento (ver diagnóstico del prompt 28: el
    stock_teorico() del producto base SÍ se recalculaba bien solo — es
    live, no cacheado — el bug real era únicamente este).
    """
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
        # Vista previa en el formulario (prompt 28, punto 2): precio/costo
        # DE HOY de cada producto seleccionable, para que el admin vea de
        # inmediato qué se aplicaría si deja los campos en blanco — el
        # cálculo real al guardar usa costo_promedio(hasta_fecha=<fecha
        # del movimiento>), que puede diferir de "hoy" si hubo compras
        # después de esa fecha; el texto en la plantilla aclara eso.
        precios = {
            str(producto.pk): {
                "nombre": producto.nombre,
                "precio": str(producto.precio_venta_actual),
                "costo": str(producto.costo_promedio()),
            }
            for producto in context["form"].fields["producto"].queryset
        }
        context["precios_productos_json"] = json.dumps(precios)
        return context

    def form_valid(self, form):
        producto_anterior_id = self._snapshot_antes["producto"]
        producto_cambio = producto_anterior_id != form.instance.producto_id

        precio_a_mano = form.cleaned_data.get("precio_venta_unitario")
        costo_a_mano = form.cleaned_data.get("costo_unitario_snapshot")

        if precio_a_mano is not None:
            form.instance.precio_venta_unitario = precio_a_mano
        elif producto_cambio and form.instance.tipo == "venta":
            form.instance.precio_venta_unitario = form.instance.producto.precio_venta_actual

        if costo_a_mano is not None:
            form.instance.costo_unitario_snapshot = costo_a_mano
        elif producto_cambio:
            form.instance.costo_unitario_snapshot = form.instance.producto.costo_promedio(
                hasta_fecha=form.instance.fecha
            )

        return super().form_valid(form)


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


class CorreccionDeleteView(RequiereRol("admin"), RequiereConexionMixin, ManejoErrorConexionMixin, generic.DeleteView):
    """
    Base compartida para eliminar un registro ya guardado. No usa el flujo
    de confirmación por defecto de DeleteView (un solo POST sin más) —
    exige motivo (MotivoCorreccionForm) y dos ganchos que cada subclase
    puede usar para la relación ConteoFisico.ajuste_generado (prompt 17,
    punto 5): _bloqueo_eliminacion() para impedirla del todo, o
    _advertencia_eliminacion() para avisar sin impedirla.

    ManejoErrorConexionMixin, igual que CorreccionUpdateView — eliminar
    historial tampoco pasa nunca por la cola offline (prompt 19).
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


class CorreccionHistorialListView(RequiereRol("admin"), RequiereConexionMixin, generic.ListView):
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
                    if campo != "motivo" and valor_antes != correccion.datos_nuevos.get(campo)
                ]
            datos_anteriores = {
                campo: valor for campo, valor in correccion.datos_anteriores.items() if campo != "motivo"
            }
            filas.append({"correccion": correccion, "cambios": cambios, "datos_anteriores": datos_anteriores})
        context["filas"] = filas
        return context

# inventario/models.py
import uuid as uuid_lib

from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, F
from decimal import Decimal


class Categoria(models.Model):
    """Categoría de bebida embotellada (Cerveza, Licor, Vino, Gaseosa, Agua, etc.)"""
    nombre = models.CharField(max_length=100, unique=True)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Categorías"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    """
    Catálogo de bebidas embotelladas auditadas. Se ingresa manualmente,
    en espejo con el menú del sistema principal (coincidencia solo por
    nombre, sin relación técnica entre los dos sistemas).
    """
    nombre = models.CharField(max_length=150, unique=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, related_name="productos")
    precio_venta_actual = models.DecimalField(max_digits=8, decimal_places=2)
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    # Relación de equivalencia: un producto derivado es el MISMO producto
    # físico que producto_base, solo empacado o cobrado distinto (ej. una
    # variante de precio, o un cubetazo de varias botellas). No tiene
    # inventario propio — factor_equivalencia dice cuántas unidades de
    # producto_base consume vender 1 unidad de este producto (ver
    # stock_teorico() y costo_promedio() más abajo, donde se resuelve el
    # descuento real contra el producto base).
    producto_base = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="derivados",
        help_text="Si este producto es una variante de precio o un paquete (ej. cubetazo) de otro, el producto base real.",
    )
    factor_equivalencia = models.PositiveIntegerField(
        default=1,
        help_text="Unidades de producto_base que consume vender 1 unidad de este producto.",
    )
    # costo_promedio_cache (prompt 19): SOLO se usa en la copia local
    # offline del catálogo (alias "local_disco", ver inventario/offline.py)
    # — contra Neon (alias "default") siempre queda en None y nunca se lee
    # ni se escribe en el uso normal online. costo_promedio() es un método
    # calculado (agrega LoteCompra), no algo que se pueda copiar tal cual
    # a la caché local sin conexión — este campo guarda el último valor
    # calculado en el refresco de caché más reciente (con conexión), para
    # que MovimientoSalidaCreateView pueda armar un costo_unitario_snapshot
    # razonable al encolar una venta sin conexión.
    costo_promedio_cache = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, editable=False)
    # stock_teorico_cache (prompt 19c): mismo patrón que costo_promedio_cache
    # — solo tiene sentido en la copia local ("local_disco"), calculado en
    # cada refresco de caché con conexión. stock_teorico() agrega
    # LoteCompra/MovimientoSalida, tablas que a propósito NO existen en el
    # alias local (ver db_router.py), así que sin este campo no habría
    # forma de validar "hay suficiente stock" al registrar una venta sin
    # conexión (prompt 19c, punto 5). SIEMPRE es el stock del producto BASE
    # incluso en la fila de un derivado — igual que costo_promedio_cache,
    # nunca el resultado de dividir por factor_equivalencia, para que
    # quien lo lea decida esa división con el factor correcto del momento.
    stock_teorico_cache = models.IntegerField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["categoria__nombre", "nombre"]

    def __str__(self):
        return self.nombre

    def clean(self):
        super().clean()
        if self.producto_base_id:
            if self.pk and self.producto_base_id == self.pk:
                raise ValidationError({"producto_base": "Un producto no puede ser su propio producto base."})
            if self.producto_base.producto_base_id is not None:
                raise ValidationError({
                    "producto_base": (
                        f"'{self.producto_base}' ya es un producto derivado de "
                        f"'{self.producto_base.producto_base}' — no se puede encadenar. "
                        f"Selecciona directamente el producto base real."
                    )
                })
            if self.factor_equivalencia < 1:
                # PositiveIntegerField permite 0 a nivel de Django, pero 0
                # aquí significaría "esta venta no consume nada del
                # producto base" (división por cero en stock_teorico()).
                raise ValidationError({"factor_equivalencia": "El factor de equivalencia debe ser al menos 1."})

    def costo_promedio(self, hasta_fecha=None):
        """
        Costo promedio ponderado. Un producto derivado no tiene compras
        propias — su costo es el del producto base, multiplicado por
        cuántas unidades base consume (factor_equivalencia).
        """
        if self.producto_base_id is not None:
            return (self.producto_base.costo_promedio(hasta_fecha) * self.factor_equivalencia).quantize(Decimal("0.01"))

        qs = self.lotes_compra.all()
        if hasta_fecha:
            qs = qs.filter(fecha__lte=hasta_fecha)
        agregado = qs.aggregate(
            total_costo=Sum(F("cantidad") * F("costo_unitario")),
            total_unidades=Sum("cantidad"),
        )
        if not agregado["total_unidades"]:
            return Decimal("0.00")
        return (agregado["total_costo"] / agregado["total_unidades"]).quantize(Decimal("0.01"))

    def stock_teorico(self, hasta_fecha=None):
        """
        Para un producto BASE (producto_base=None): inventario real —
        compras propias, menos salidas propias, menos las salidas de cada
        producto derivado de este (ventas/mermas/ajustes registrados bajo
        el nombre del derivado, convertidas a unidades base con su
        factor_equivalencia). Esta es la fuente de verdad del stock físico.

        Para un producto DERIVADO (producto_base != None): no tiene
        inventario propio real. Lo que se devuelve aquí es solo
        INFORMATIVO — el stock del producto base traducido a "cuántos de
        este derivado alcanzarían" (división entera) — nunca una fuente
        de verdad independiente.
        """
        if self.producto_base_id is not None:
            stock_base = self.producto_base.stock_teorico(hasta_fecha)
            return stock_base // self.factor_equivalencia

        compras = self.lotes_compra.all()
        salidas = self.movimientos_salida.all()
        if hasta_fecha:
            compras = compras.filter(fecha__lte=hasta_fecha)
            salidas = salidas.filter(fecha__lte=hasta_fecha)
        total_compras = compras.aggregate(t=Sum("cantidad"))["t"] or 0
        total_salidas = salidas.aggregate(t=Sum("cantidad"))["t"] or 0

        total_salidas_derivados = 0
        for derivado in self.derivados.all():
            salidas_derivado = derivado.movimientos_salida.all()
            if hasta_fecha:
                salidas_derivado = salidas_derivado.filter(fecha__lte=hasta_fecha)
            cantidad_derivado = salidas_derivado.aggregate(t=Sum("cantidad"))["t"] or 0
            total_salidas_derivados += cantidad_derivado * derivado.factor_equivalencia

        return total_compras - total_salidas - total_salidas_derivados


class LoteCompra(models.Model):
    """Cada ingreso manual de inventario (ej: '1000 Gallo el 24/08')."""
    # uuid (prompt 19): identifica el registro de forma única desde el
    # momento en que se crea LOCALMENTE, antes de que llegue a la nube —
    # la cola de sincronización offline lo usa como clave para que un
    # reintento nunca cree un duplicado remoto (ver inventario/offline.py).
    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="lotes_compra")
    fecha = models.DateField()
    cantidad = models.PositiveIntegerField()
    costo_unitario = models.DecimalField(max_digits=8, decimal_places=2)
    proveedor = models.CharField(max_length=150, blank=True, null=True)
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    notas = models.TextField(blank=True, null=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha", "-creado_en"]

    def clean(self):
        super().clean()
        if self.producto_id and self.producto.producto_base_id is not None:
            raise ValidationError({
                "producto": (
                    f"'{self.producto}' es una variante de '{self.producto.producto_base}' — "
                    f"registra las compras sobre '{self.producto.producto_base}', no sobre esta variante."
                )
            })

    def __str__(self):
        return f"{self.producto} +{self.cantidad} ({self.fecha})"


class MovimientoSalida(models.Model):
    """
    Toda salida de inventario: venta diaria, merma o ajuste de conteo físico.
    Precio y costo se guardan como snapshot al momento del registro, para que
    los reportes históricos no cambien si el costo promedio cambia después.
    """
    TIPO_CHOICES = (
        ("venta", "Venta"),
        ("merma", "Merma / Pérdida"),
        ("ajuste", "Ajuste por conteo físico"),
    )
    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="movimientos_salida")
    fecha = models.DateField()
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES, default="venta")
    cantidad = models.IntegerField(
        help_text=(
            "Para venta/merma siempre positivo. Para ajuste: positivo si el "
            "conteo físico encontró un faltante (resta stock), negativo si "
            "encontró un sobrante (suma stock) — ver generar_ajuste()."
        )
    )
    precio_venta_unitario = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    costo_unitario_snapshot = models.DecimalField(max_digits=8, decimal_places=2)
    motivo = models.CharField(max_length=255, blank=True, null=True)  # obligatorio en mermas/ajustes, validar en el form
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha", "-creado_en"]

    def save(self, *args, **kwargs):
        if not self.costo_unitario_snapshot:
            self.costo_unitario_snapshot = self.producto.costo_promedio(hasta_fecha=self.fecha)
        if self.tipo == "venta" and not self.precio_venta_unitario:
            self.precio_venta_unitario = self.producto.precio_venta_actual
        super().save(*args, **kwargs)

    def __str__(self):
        if self.cantidad < 0:
            return f"{self.producto} +{abs(self.cantidad)} ({self.get_tipo_display()}, {self.fecha})"
        return f"{self.producto} -{self.cantidad} ({self.get_tipo_display()}, {self.fecha})"


class ConteoFisico(models.Model):
    """
    Conteo físico periódico (recomendado: semanal). Compara el stock teórico
    contra lo contado a mano; si hay diferencia, permite generar un
    MovimientoSalida tipo 'ajuste' para cuadrar el sistema y dejar rastro.
    """
    uuid = models.UUIDField(default=uuid_lib.uuid4, unique=True, editable=False)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="conteos_fisicos")
    fecha = models.DateField()
    cantidad_contada = models.PositiveIntegerField()
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    notas = models.TextField(blank=True, null=True)
    # OneToOneField = ForeignKey(unique=True) (prompt 21): capa de
    # protección a nivel de base de datos, independiente del código —
    # nunca deja que dos ConteoFisico distintos terminen apuntando al
    # mismo MovimientoSalida, sin importar por dónde se haya escrito.
    # Postgres permite múltiples NULL en una columna única (el caso
    # normal: la mayoría de conteos no tienen ajuste todavía), así que
    # esto no cambia nada del comportamiento actual — solo cierra una vía
    # de corrupción. No previene por sí sola la condición de carrera
    # original (un mismo conteo generando dos MovimientoSalida distintos)
    # — eso lo resuelve select_for_update() en generar_ajuste() (ver
    # views.py).
    ajuste_generado = models.OneToOneField(MovimientoSalida, on_delete=models.SET_NULL, null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if self.producto_id and self.producto.producto_base_id is not None:
            raise ValidationError({
                "producto": (
                    f"'{self.producto}' es una variante/paquete de '{self.producto.producto_base}' — "
                    f"registra el conteo físico sobre '{self.producto.producto_base}', no sobre esta variante."
                )
            })

    @property
    def diferencia(self):
        teorico = self.producto.stock_teorico(hasta_fecha=self.fecha)
        return self.cantidad_contada - teorico

    def __str__(self):
        return f"Conteo {self.producto} ({self.fecha}): {self.cantidad_contada}"


class CorreccionHistorial(models.Model):
    """
    Registro obligatorio de cada edición o eliminación de un LoteCompra,
    MovimientoSalida o ConteoFisico ya guardado (prompt 17). Cambia
    conscientemente la regla original de que un reporte de un mes cerrado
    nunca cambia: ahora sí puede cambiar si un admin corrige algo, pero
    siempre queda constancia de quién, cuándo, qué y por qué — nunca se
    crea uno de estos sin el cambio real aplicado en la misma transacción,
    ni viceversa (ver las vistas de corrección en views.py).
    """
    ACCION_CHOICES = (("edicion", "Edición"), ("eliminacion", "Eliminación"))

    tipo_registro = models.CharField(max_length=30)  # "LoteCompra", "MovimientoSalida", "ConteoFisico"
    registro_id = models.PositiveIntegerField()
    accion = models.CharField(max_length=15, choices=ACCION_CHOICES)
    datos_anteriores = models.JSONField()
    datos_nuevos = models.JSONField(null=True, blank=True)
    motivo = models.TextField()
    realizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Correcciones al historial"
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.get_accion_display()} {self.tipo_registro} #{self.registro_id} ({self.fecha:%Y-%m-%d})"


class ReferenciaVentaImportada(models.Model):
    """
    Opcional (fase posterior, no implementar todavía en el flujo funcional):
    importación de un archivo de ventas exportado manualmente del sistema
    principal, solo como referencia de comparación. Nunca escribe ni
    modifica el stock del sistema de auditoría.
    """
    producto_nombre = models.CharField(max_length=150)
    fecha = models.DateField()
    cantidad_reportada = models.PositiveIntegerField()
    archivo_origen = models.CharField(max_length=255)
    importado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.producto_nombre} ({self.fecha}): {self.cantidad_reportada} [ref]"


# --- Modelos exclusivamente locales (prompt 19, motor de sincronización
# offline) — NUNCA existen en "default" (Neon): OfflineRouter
# (inventario/db_router.py) impide que se migren ahí. Solo viven en
# "local_disco" (el archivo SQLite local de esta máquina). Se definen
# aquí, junto al resto de modelos del app, en vez de un archivo aparte,
# para no romper la convención de un solo models.py del proyecto — el
# router es lo que de verdad los mantiene fuera de Neon.

class PendienteSincronizacion(models.Model):
    """
    Una escritura (LoteCompra/MovimientoSalida/ConteoFisico) que ya se
    guardó localmente pero todavía no se confirmó contra Neon — el
    "outbox" del patrón. Se crea en ColaOfflineMixin.form_valid() justo
    antes de intentar la escritura real, y se borra apenas esa escritura
    se confirma (o la confirma un reintento posterior del hilo de
    sincronización) — nunca se marca "sincronizado" en la misma fila,
    simplemente deja de existir.

    payload guarda los campos ya listos para
    Modelo.objects.get_or_create(uuid=..., defaults=payload) — uuid es la
    clave de idempotencia: un reintento que en realidad ya se había
    confirmado (ej. la escritura remota funcionó pero la respuesta se
    perdió) encuentra la fila existente por uuid en vez de duplicarla.

    Vive en el archivo local de esta máquina para los TRES roles (prompt
    19b, punto 3) — antes la cola del vendedor vivía solo en RAM y se
    perdía al cerrar la app; esa decisión se revirtió explícitamente.
    """
    uuid = models.UUIDField(unique=True)
    modelo = models.CharField(max_length=30)  # "LoteCompra" | "MovimientoSalida" | "ConteoFisico"
    payload = models.JSONField()
    creado_en = models.DateTimeField()  # momento de creación LOCAL — nunca el de sincronización (prompt 29 aplica a esta fecha)
    intentos = models.PositiveIntegerField(default=0)
    ultimo_error = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["creado_en"]

    def __str__(self):
        return f"Pendiente {self.modelo} {self.uuid} ({self.intentos} intentos)"


class CredencialOfflineCache(models.Model):
    """
    Credenciales del último inicio de sesión exitoso de cada usuario que
    alguna vez entró CON CONEXIÓN en esta máquina (prompt 19b, punto 1).
    Es lo que permite iniciar sesión durante un apagón: sin esto, abrir o
    reiniciar la app sin internet dejaba a todos afuera y el modo offline
    entero quedaba inservible.

    password_hash guarda el MISMO hash que Django tiene en
    auth_user.password (PBKDF2 con sal) — nunca la contraseña en texto
    plano. check_password() lo valida igual que contra la base real, y de
    este archivo no se puede deducir la contraseña.

    aviso_password_cambiada lo marca refrescar_credenciales_cache() (hilo
    de fondo) cuando, al volver la conexión, el hash real en Neon ya no
    coincide con el cacheado: la contraseña cambió mientras la máquina
    estaba sin conexión. Ver ContinuidadSesionOfflineMiddleware en
    inventario/offline.py para qué se hace con esa marca.
    """
    username = models.CharField(max_length=150, primary_key=True)
    user_id = models.PositiveIntegerField(db_index=True)
    password_hash = models.CharField(max_length=255)
    rol = models.CharField(max_length=20)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    aviso_password_cambiada = models.BooleanField(default=False)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return f"{self.username} ({self.rol})"


class MovimientoHistorialCache(models.Model):
    """
    Copia local, de solo lectura, de los últimos movimientos ya
    confirmados en Neon (prompt 19c, punto 1) — permite consultar
    Historial sin conexión, combinada con la cola de pendientes de esta
    misma máquina (PendienteSincronizacion) para mostrar también lo
    registrado localmente y aún no sincronizado.

    Se recalcula por completo (borra y reinserta) en cada refresco de
    caché con conexión — ver refrescar_historial_cache() en
    inventario/offline.py — y se limita a los LIMITE_HISTORIAL_CACHE
    movimientos más recientes: el alcance explícito de esta caché es
    "los últimos movimientos", no el historial completo (que sigue
    necesitando conexión para un rango arbitrario, ver HistorialView).

    payload guarda la fila ya lista para mostrar en la plantilla
    (mismo formato que movimientos_periodo() en services.py, con el
    producto ya resuelto a su nombre en vez de la instancia, y
    fecha/valor_unitario/creado_en serializados) — evita tener que volver
    a resolver relaciones al leer, y evita duplicar esta tabla con
    columnas para cada campo que la plantilla necesita.

    producto_id y fecha SÍ son columnas propias (no solo parte del
    payload): son los únicos dos criterios por los que Historial filtra
    (HistorialFiltroForm), y esas columnas permiten filtrar con el ORM en
    vez de traer todo a Python.
    """
    tipo_registro = models.CharField(max_length=30)  # "LoteCompra" | "MovimientoSalida" | "ConteoFisico"
    registro_id = models.PositiveIntegerField()
    fecha = models.DateField()
    producto_id = models.PositiveIntegerField(db_index=True)
    payload = models.JSONField()

    class Meta:
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.tipo_registro} #{self.registro_id} ({self.fecha})"

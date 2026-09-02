from django import forms
from django.utils import timezone

from .models import Categoria, ConteoFisico, LoteCompra, MovimientoSalida, Producto
from .offline import hay_conexion
from .services import OPCIONES_TIPO_MOVIMIENTO


def _alias_catalogo():
    """
    "default" (Neon) si hay conexión, "local_disco" (catálogo cacheado,
    prompt 19) si no — así el desplegable de producto de los 3
    formularios de escritura sigue funcionando sin conexión, en vez de
    reventar al intentar validar contra Neon. Nunca aplica a los
    formularios de CORRECCIÓN de historial (LoteCompraCorreccionForm,
    etc. heredan estos mismos __init__, pero editar historial siempre
    exige conexión activa de todas formas — ver CorreccionUpdateView).
    """
    return "default" if hay_conexion() else "local_disco"


def _opciones_usuario():
    """
    Pares (id como string, username) para el desplegable "Usuario" de
    Historial (prompt 35), en la misma fuente que ya usa _alias_catalogo:
    auth.User real con conexión, CredencialOfflineCache sin ella —
    auth.User no existe en los alias locales (ver db_router.py), así que
    no hay forma de armar un ModelChoiceField único que sirva en los dos
    casos. Se usa el id (no el username) como VALUE del <option> porque
    es lo que de verdad se guarda en registrado_por_id/usuario_id en
    todas las filas de historial, online y offline por igual — y
    CredencialOfflineCache.user_id es exactamente ese mismo id, no uno
    aparte, así que el resultado es el mismo filtro sin importar cuál de
    las dos fuentes lo armó.
    """
    if hay_conexion():
        from django.contrib.auth.models import User

        return [(str(u.pk), u.username) for u in User.objects.order_by("username")]

    from .models import CredencialOfflineCache

    filas = CredencialOfflineCache.objects.using("local_disco").order_by("username")
    return [(str(c.user_id), c.username) for c in filas]


def _queryset_producto_activo_o_actual(instance):
    """
    Solo productos activos, más el producto ya asignado a esta instancia
    si se está editando un registro existente que referencia uno ya
    desactivado (prompt 22, punto 4.4) — así un producto desactivado
    nunca aparece como opción para un registro NUEVO, pero un registro ya
    guardado que lo referencia se sigue pudiendo ver/editar sin que el
    formulario lo rechace como "opción inválida".
    """
    alias = _alias_catalogo()
    queryset = Producto.objects.using(alias).filter(activo=True)
    if instance.pk and instance.producto_id:
        queryset = queryset | Producto.objects.using(alias).filter(pk=instance.producto_id)
    return queryset


def _queryset_producto_base_activo_o_actual(instance):
    """
    Como _queryset_producto_activo_o_actual(), pero además excluye
    productos derivados (prompt 28b) — para LoteCompra y ConteoFisico,
    que solo tienen sentido sobre el producto base real: un derivado no
    tiene compras propias (Producto.clean() ya lo bloqueaba al enviar,
    desde el prompt 15) ni conteo físico propio (ConteoFisico.clean(),
    prompt 28b) — ahora tampoco aparece como opción para elegir.
    """
    alias = _alias_catalogo()
    queryset = Producto.objects.using(alias).filter(activo=True, producto_base__isnull=True)
    if instance.pk and instance.producto_id:
        queryset = queryset | Producto.objects.using(alias).filter(pk=instance.producto_id)
    return queryset


MENSAJE_PRODUCTO_DERIVADO_NO_VALIDO = (
    "Este producto es una variante/paquete de otro (un producto base) — "
    "regístralo sobre el producto base, no sobre esta variante."
)


class CategoriaForm(forms.ModelForm):
    class Meta:
        model = Categoria
        fields = ["nombre", "activo"]


class ProductoForm(forms.ModelForm):
    class Meta:
        model = Producto
        fields = [
            "nombre",
            "categoria",
            "precio_venta_actual",
            "activo",
            "producto_base",
            "factor_equivalencia",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Solo un producto base real (no otro derivado) puede elegirse como
        # producto_base — evita encadenar derivados desde el propio formulario
        # (Producto.clean() lo bloquearía de todas formas, pero así ni
        # siquiera aparece como opción). Tampoco puede elegirse a sí mismo.
        queryset_base = Producto.objects.filter(producto_base__isnull=True)
        if self.instance.pk:
            queryset_base = queryset_base.exclude(pk=self.instance.pk)
        self.fields["producto_base"].queryset = queryset_base
        # empty_label explícito (prompt 37): Django 6 estrenó la constante
        # BLANK_CHOICE_LABEL y todavía no viene traducida al español, así
        # que estos desplegables mostraban "- Select an option -" en
        # inglés en medio de pantallas que están todas en español.
        self.fields["categoria"].empty_label = "Selecciona una categoría"
        self.fields["producto_base"].empty_label = "Ninguno — es un producto base"


class LoteCompraForm(forms.ModelForm):
    class Meta:
        model = LoteCompra
        fields = ["producto", "fecha", "cantidad", "costo_unitario", "proveedor", "notas"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}),
            "notas": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["producto"].queryset = _queryset_producto_base_activo_o_actual(self.instance)
        # empty_label explícito (prompt 37): Django 6 estrenó la constante
        # BLANK_CHOICE_LABEL y todavía no viene traducida al español, así
        # que estos desplegables mostraban "- Select an option -" en
        # inglés en medio de pantallas que están todas en español.
        self.fields["producto"].empty_label = "Selecciona un producto"
        # El selector ya oculta los derivados (ver el queryset de arriba),
        # pero si de todas formas llega uno en el POST (HTML manipulado a
        # mano, o un registro ya editado que apuntaba a un derivado desde
        # antes), el mensaje de rechazo de Django ("Escoja una opción
        # válida...") no explica el motivo — este sí (prompt 28b, punto 1).
        self.fields["producto"].error_messages["invalid_choice"] = MENSAJE_PRODUCTO_DERIVADO_NO_VALIDO


class MovimientoSalidaForm(forms.ModelForm):
    class Meta:
        model = MovimientoSalida
        fields = ["producto", "fecha", "tipo", "cantidad", "motivo"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}),
            # Se renderiza como control segmentado (pastillas) en la
            # plantilla en vez de un <select>, para que sea más claro con
            # un vistazo qué tipo de salida se está registrando.
            "tipo": forms.RadioSelect,
        }

    def __init__(self, *args, permitir_todos_los_tipos=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["producto"].queryset = _queryset_producto_activo_o_actual(self.instance)
        # empty_label explícito (prompt 37): Django 6 estrenó la constante
        # BLANK_CHOICE_LABEL y todavía no viene traducida al español, así
        # que este desplegable mostraba "- Select an option -" en inglés.
        self.fields["producto"].empty_label = "Selecciona un producto"
        self.permitir_todos_los_tipos = permitir_todos_los_tipos
        if not permitir_todos_los_tipos:
            # Un vendedor solo puede registrar ventas: se restringe la
            # única opción mostrada (no solo se oculta con CSS) y clean()
            # de todas formas rechaza cualquier otro valor que llegue en
            # el POST, así se haya manipulado el HTML a mano.
            self.fields["tipo"].choices = [("venta", "Venta")]
            self.fields["tipo"].initial = "venta"
            # Un vendedor tampoco puede elegir la fecha (prompt 29):
            # siempre es la de hoy — se oculta el campo (queda como
            # hidden con el valor de hoy) en vez de quitarlo del todo,
            # así el form sigue siendo válido sin campos faltantes. La
            # aplicación real de "siempre hoy" pasa en el backend (ver
            # MovimientoSalidaCreateView.form_valid) — esto es solo la
            # capa visual, un HTML manipulado a mano no basta para
            # cambiarla.
            self.fields["fecha"].widget = forms.HiddenInput()
            self.fields["fecha"].initial = timezone.localdate()

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get("tipo")
        motivo = cleaned_data.get("motivo")
        cantidad = cleaned_data.get("cantidad")
        producto = cleaned_data.get("producto")

        if not self.permitir_todos_los_tipos and tipo != "venta":
            self.add_error("tipo", "Como vendedor, solo puedes registrar ventas.")

        if tipo in ("merma", "ajuste") and not motivo and "motivo" not in self.errors:
            # El "not in self.errors" evita un segundo mensaje redundante
            # cuando motivo ya es obligatorio a nivel de campo (ver
            # MovimientoSalidaCorreccionForm, que lo vuelve obligatorio
            # siempre porque ahí dobla como motivo de la corrección).
            self.add_error(
                "motivo",
                "El motivo es obligatorio para mermas y ajustes por conteo físico.",
            )

        if cantidad is not None:
            if tipo in ("venta", "merma") and cantidad <= 0:
                self.add_error("cantidad", "La cantidad debe ser mayor que cero.")
            elif tipo == "ajuste" and cantidad == 0:
                self.add_error("cantidad", "La cantidad del ajuste no puede ser cero.")

        # Validación de stock disponible (prompt 19c, punto 5): solo para
        # una venta NUEVA (self.instance.pk is None) — nunca al CORREGIR
        # una ya guardada (MovimientoSalidaCorreccionForm hereda este
        # clean()), porque ahí stock_disponible_para_venta() ya cuenta la
        # cantidad ORIGINAL como consumida, y validar como si fuera una
        # venta nueva de la cantidad corregida exigiría de más. Tampoco
        # aplica a merma/ajuste — el reporte que motivó este punto es
        # específicamente "se puede vender más de lo que hay".
        if (
            self.instance.pk is None
            and tipo == "venta"
            and producto is not None
            and cantidad is not None
            and cantidad > 0
            and "cantidad" not in self.errors
        ):
            from .offline import stock_disponible_para_venta

            disponible = stock_disponible_para_venta(producto)
            if cantidad > disponible:
                self.add_error(
                    "cantidad",
                    f"No hay suficiente inventario disponible: quedan {max(disponible, 0)} unidades.",
                )

        return cleaned_data


class ConteoFisicoForm(forms.ModelForm):
    class Meta:
        model = ConteoFisico
        fields = ["producto", "fecha", "cantidad_contada", "notas"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}),
            "notas": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["producto"].queryset = _queryset_producto_base_activo_o_actual(self.instance)
        # Ver el mismo comentario en LoteCompraForm.__init__ sobre empty_label.
        self.fields["producto"].empty_label = "Selecciona un producto"
        # Ver el mismo comentario en LoteCompraForm.__init__ — mensaje
        # claro (prompt 28b, punto 1) incluso si un derivado llega en el
        # POST más allá de lo que ya oculta el selector.
        self.fields["producto"].error_messages["invalid_choice"] = MENSAJE_PRODUCTO_DERIVADO_NO_VALIDO


class HistorialFiltroForm(forms.Form):
    # A propósito NO se restringe a activo=True: es un filtro de
    # búsqueda sobre el histórico, no un formulario de registrar algo
    # nuevo — se debe poder seguir filtrando por un producto ya
    # desactivado para ver su historial completo (ver prompt 22, punto 4.4,
    # que sí restringe los formularios de ESCRITURA).
    producto = forms.ModelChoiceField(
        queryset=Producto.objects.all(),
        required=False,
        # empty_label explícito (prompt 37): sin esto Django pone su
        # texto por defecto, que sale en INGLÉS ("- Select an option -")
        # en medio de una pantalla enteramente en español. Los otros dos
        # filtros ya decían "Todos los tipos" / "Todos los usuarios";
        # este se había quedado atrás.
        empty_label="Todos los productos",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    # ChoiceField llano, no ModelChoiceField (prompt 35): sin conexión no
    # hay forma de tener un queryset de auth.User —ese modelo no existe
    # en los alias locales, ver db_router.py, y "usuario" es justo uno de
    # los filtros que Historial debe seguir ofreciendo sin conexión—, así
    # que las opciones se arman a mano en __init__() desde CUALQUIERA de
    # las dos fuentes (User real, o CredencialOfflineCache), siempre como
    # pares (id como string, username). El id es el mismo en los dos
    # casos porque CredencialOfflineCache.user_id ES el pk real de User.
    tipo = forms.ChoiceField(
        choices=OPCIONES_TIPO_MOVIMIENTO,
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    usuario = forms.ChoiceField(
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    fecha_desde = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    fecha_hasta = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Historial funciona sin conexión (prompt 19c, punto 1) — el
        # desplegable de producto tiene que poder armarse desde el
        # catálogo cacheado ahí también, igual que en los 3 formularios
        # de escritura (ver _alias_catalogo()).
        self.fields["producto"].queryset = Producto.objects.using(_alias_catalogo()).all()
        self.fields["usuario"].choices = [("", "Todos los usuarios")] + _opciones_usuario()

    def clean_usuario(self):
        # ChoiceField.clean() ya rechaza cualquier valor que no esté en
        # choices —construidas en __init__() desde los usuarios reales—,
        # así que llegar aquí con algo no vacío ya es un id válido.
        valor = self.cleaned_data.get("usuario")
        return int(valor) if valor else None

    def clean(self):
        cleaned_data = super().clean()
        fecha_desde = cleaned_data.get("fecha_desde")
        fecha_hasta = cleaned_data.get("fecha_hasta")
        if fecha_desde and fecha_hasta and fecha_desde > fecha_hasta:
            self.add_error("fecha_hasta", "La fecha final no puede ser anterior a la fecha inicial.")
        return cleaned_data


class MotivoCorreccionForm(forms.Form):
    """
    Motivo obligatorio de una corrección administrativa (edición o
    eliminación de un registro ya guardado, prompt 17) — separado de
    cualquier campo "motivo"/"notas" propio del modelo (ese es el motivo
    de la venta/merma/ajuste original, no el de por qué se está
    corrigiendo ahora). CharField sin required=False, así que Django ya
    rechaza un envío vacío por su cuenta.
    """
    motivo_correccion = forms.CharField(
        label="Motivo de la corrección",
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
        help_text="Obligatorio: explica por qué se corrige o elimina este registro ya guardado.",
    )


class LoteCompraCorreccionForm(LoteCompraForm):
    motivo_correccion = forms.CharField(
        label="Motivo de la corrección",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Obligatorio: explica por qué se corrige este registro ya guardado.",
    )


class MovimientoSalidaCorreccionForm(MovimientoSalidaForm):
    """
    A diferencia de LoteCompraCorreccionForm/ConteoFisicoCorreccionForm,
    NO agrega un campo "motivo_correccion" aparte: MovimientoSalida ya
    tiene su propio campo "motivo" (heredado de MovimientoSalidaForm), y
    al editar un movimiento ya guardado, el motivo de por qué se
    corrige ahora y el motivo del movimiento en sí son, en la práctica,
    la misma explicación — pedirla dos veces era una duplicación
    confusa en el formulario. Se reutiliza el campo "motivo" existente,
    volviéndolo obligatorio siempre (antes solo lo era para merma/ajuste)
    y relabeleado para dejar claro que alimenta la corrección — sigue
    siendo el mismo campo del modelo, así que la venta/merma/ajuste
    corregida se sigue guardando con su motivo real.

    precio_venta_unitario/costo_unitario_snapshot (prompt 28): NO son
    campos del Meta de MovimientoSalidaForm (no se pueden editar al
    registrar una venta nueva, donde siempre los calcula
    MovimientoSalida.save()) — aquí se agregan explícitamente, ambos
    opcionales, como el override manual que puede escribir el admin. Si
    se dejan en blanco, MovimientoSalidaCorreccionUpdateView.form_valid()
    decide: si el producto del movimiento cambió, los recalcula al
    precio/costo del producto NUEVO; si el producto no cambió, los deja
    tal como estaban (una corrección de, por ejemplo, solo la fecha o la
    cantidad no debe alterar por accidente el costo histórico
    congelado). Si el admin escribe un valor aquí, se respeta tal cual,
    haya cambiado el producto o no.
    """
    precio_venta_unitario = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        label="Precio de venta unitario (Q)",
        help_text=(
            "Solo aplica a ventas. Si cambias el producto de arriba y dejas esto en "
            "blanco, se recalcula automáticamente al precio de venta actual del "
            "producto NUEVO. Si no cambias el producto, se deja igual. Escribe un "
            "valor aquí solo si quieres forzarlo a mano."
        ),
    )
    costo_unitario_snapshot = forms.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        label="Costo unitario (Q)",
        help_text=(
            "Si cambias el producto de arriba y dejas esto en blanco, se recalcula "
            "automáticamente al costo promedio del producto NUEVO a la fecha del "
            "movimiento. Si no cambias el producto, se deja igual. Escribe un valor "
            "aquí solo si quieres forzarlo a mano."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["motivo"].required = True
        self.fields["motivo"].label = "Motivo de la corrección"
        self.fields["motivo"].help_text = (
            "Obligatorio: explica por qué se corrige este registro ya guardado."
        )


class ConteoFisicoCorreccionForm(ConteoFisicoForm):
    motivo_correccion = forms.CharField(
        label="Motivo de la corrección",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Obligatorio: explica por qué se corrige este registro ya guardado.",
    )


class ReporteForm(forms.Form):
    fecha_inicio = forms.DateField(
        required=False,
        label="Desde",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    fecha_fin = forms.DateField(
        required=False,
        label="Hasta",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    # Sin selección = todos los productos activos (ver ReporteView). El
    # widget real se dibuja a mano en la plantilla, agrupado por
    # categoría, así que este campo no se renderiza con crispy.
    productos = forms.ModelMultipleChoiceField(
        queryset=Producto.objects.filter(activo=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def clean(self):
        cleaned_data = super().clean()
        fecha_inicio = cleaned_data.get("fecha_inicio")
        fecha_fin = cleaned_data.get("fecha_fin")
        if fecha_inicio and fecha_fin and fecha_inicio > fecha_fin:
            self.add_error("fecha_fin", "La fecha final no puede ser anterior a la fecha inicial.")
        return cleaned_data

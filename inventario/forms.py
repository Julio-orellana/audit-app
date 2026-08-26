from django import forms

from .models import Categoria, ConteoFisico, LoteCompra, MovimientoSalida, Producto


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


class LoteCompraForm(forms.ModelForm):
    class Meta:
        model = LoteCompra
        fields = ["producto", "fecha", "cantidad", "costo_unitario", "proveedor", "notas"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}),
            "notas": forms.Textarea(attrs={"rows": 3}),
        }


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

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get("tipo")
        motivo = cleaned_data.get("motivo")
        cantidad = cleaned_data.get("cantidad")

        if tipo in ("merma", "ajuste") and not motivo:
            self.add_error(
                "motivo",
                "El motivo es obligatorio para mermas y ajustes por conteo físico.",
            )

        if cantidad is not None:
            if tipo in ("venta", "merma") and cantidad <= 0:
                self.add_error("cantidad", "La cantidad debe ser mayor que cero.")
            elif tipo == "ajuste" and cantidad == 0:
                self.add_error("cantidad", "La cantidad del ajuste no puede ser cero.")

        return cleaned_data


class ConteoFisicoForm(forms.ModelForm):
    class Meta:
        model = ConteoFisico
        fields = ["producto", "fecha", "cantidad_contada", "notas"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}),
            "notas": forms.Textarea(attrs={"rows": 3}),
        }


class HistorialFiltroForm(forms.Form):
    producto = forms.ModelChoiceField(
        queryset=Producto.objects.all(),
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

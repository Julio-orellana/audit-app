# inventario/models.py
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

    # Equivalencia para paquetes (ej. cubetazos): si este producto es un
    # paquete de varias unidades de otro producto, producto_base indica cuál
    # y unidades_por_paquete cuántas unidades físicas contiene. El descuento
    # automático del stock del producto base al vender un paquete se
    # implementa en el flujo de registro de ventas (no en este modelo).
    producto_base = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paquetes",
        help_text="Si este producto es un paquete (ej. cubetazo), el producto individual que contiene.",
    )
    unidades_por_paquete = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Cantidad de unidades del producto_base que contiene este paquete.",
    )

    class Meta:
        ordering = ["categoria__nombre", "nombre"]

    def __str__(self):
        return self.nombre

    def costo_promedio(self, hasta_fecha=None):
        """Costo promedio ponderado de las compras registradas hasta una fecha (o todas)."""
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
        """Balance teórico = total comprado - total salido (ventas + mermas + ajustes), a una fecha dada."""
        compras = self.lotes_compra.all()
        salidas = self.movimientos_salida.all()
        if hasta_fecha:
            compras = compras.filter(fecha__lte=hasta_fecha)
            salidas = salidas.filter(fecha__lte=hasta_fecha)
        total_compras = compras.aggregate(t=Sum("cantidad"))["t"] or 0
        total_salidas = salidas.aggregate(t=Sum("cantidad"))["t"] or 0
        return total_compras - total_salidas


class LoteCompra(models.Model):
    """Cada ingreso manual de inventario (ej: '1000 Gallo el 24/08')."""
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
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="conteos_fisicos")
    fecha = models.DateField()
    cantidad_contada = models.PositiveIntegerField()
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    notas = models.TextField(blank=True, null=True)
    ajuste_generado = models.ForeignKey(MovimientoSalida, on_delete=models.SET_NULL, null=True, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)

    @property
    def diferencia(self):
        teorico = self.producto.stock_teorico(hasta_fecha=self.fecha)
        return self.cantidad_contada - teorico

    def __str__(self):
        return f"Conteo {self.producto} ({self.fecha}): {self.cantidad_contada}"


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

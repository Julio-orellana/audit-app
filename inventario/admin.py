from django.contrib import admin

from .models import (
    Categoria,
    Producto,
    LoteCompra,
    MovimientoSalida,
    ConteoFisico,
    ReferenciaVentaImportada,
)


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre",)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        "nombre",
        "categoria",
        "precio_venta_actual",
        "activo",
        "producto_base",
        "unidades_por_paquete",
    )
    list_filter = ("categoria", "activo")
    search_fields = ("nombre",)


@admin.register(LoteCompra)
class LoteCompraAdmin(admin.ModelAdmin):
    list_display = ("producto", "fecha", "cantidad", "costo_unitario", "proveedor")
    list_filter = ("producto", "fecha")
    date_hierarchy = "fecha"


@admin.register(MovimientoSalida)
class MovimientoSalidaAdmin(admin.ModelAdmin):
    list_display = ("producto", "fecha", "tipo", "cantidad", "costo_unitario_snapshot")
    list_filter = ("tipo", "producto", "fecha")
    date_hierarchy = "fecha"


@admin.register(ConteoFisico)
class ConteoFisicoAdmin(admin.ModelAdmin):
    list_display = ("producto", "fecha", "cantidad_contada", "diferencia")
    list_filter = ("producto", "fecha")
    date_hierarchy = "fecha"


@admin.register(ReferenciaVentaImportada)
class ReferenciaVentaImportadaAdmin(admin.ModelAdmin):
    list_display = ("producto_nombre", "fecha", "cantidad_reportada", "archivo_origen")
    list_filter = ("fecha",)

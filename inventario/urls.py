from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("categorias/", views.CategoriaListView.as_view(), name="categoria_list"),
    path("categorias/nueva/", views.CategoriaCreateView.as_view(), name="categoria_create"),
    path("categorias/<int:pk>/editar/", views.CategoriaUpdateView.as_view(), name="categoria_update"),
    path("categorias/<int:pk>/toggle/", views.categoria_toggle_activo, name="categoria_toggle"),
    path("productos/", views.ProductoListView.as_view(), name="producto_list"),
    path("productos/nuevo/", views.ProductoCreateView.as_view(), name="producto_create"),
    path("productos/<int:pk>/editar/", views.ProductoUpdateView.as_view(), name="producto_update"),
    path("productos/<int:pk>/toggle/", views.producto_toggle_activo, name="producto_toggle"),
    path("lotes/nuevo/", views.LoteCompraCreateView.as_view(), name="lotecompra_create"),
    path("lotes/<int:pk>/editar/", views.LoteCompraCorreccionUpdateView.as_view(), name="lotecompra_correccion_editar"),
    path("lotes/<int:pk>/eliminar/", views.LoteCompraCorreccionDeleteView.as_view(), name="lotecompra_correccion_eliminar"),
    path("salidas/nueva/", views.MovimientoSalidaCreateView.as_view(), name="movimientosalida_create"),
    path("salidas/<int:pk>/editar/", views.MovimientoSalidaCorreccionUpdateView.as_view(), name="movimientosalida_correccion_editar"),
    path("salidas/<int:pk>/eliminar/", views.MovimientoSalidaCorreccionDeleteView.as_view(), name="movimientosalida_correccion_eliminar"),
    path("conteos/nuevo/", views.ConteoFisicoCreateView.as_view(), name="conteofisico_create"),
    path("conteos/<int:pk>/", views.ConteoFisicoDetailView.as_view(), name="conteofisico_detail"),
    path("conteos/<int:pk>/generar-ajuste/", views.generar_ajuste, name="conteofisico_generar_ajuste"),
    path("conteos/<int:pk>/editar/", views.ConteoFisicoCorreccionUpdateView.as_view(), name="conteofisico_correccion_editar"),
    path("conteos/<int:pk>/eliminar/", views.ConteoFisicoCorreccionDeleteView.as_view(), name="conteofisico_correccion_eliminar"),
    path("historial/", views.HistorialView.as_view(), name="historial"),
    path("correcciones/", views.CorreccionHistorialListView.as_view(), name="correcciones_historial"),
    path("reportes/", views.ReporteView.as_view(), name="reportes"),
    path("instrucciones/", views.InstruccionesView.as_view(), name="instrucciones"),
]

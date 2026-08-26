# inventario/sync.py
"""
Punto de enganche para el motor de sincronización offline (prompt 18,
punto 8 — preparado aquí, implementado en el prompt 19). Todavía no hace
nada: este módulo y su conexión en apps.py son deliberadamente el único
cambio de este prompt en esa dirección, para que el prompt 19 tenga dónde
escribir sin tocar ninguna vista ni formulario ya hecho.

Por qué señales de Django (post_save/post_delete) y no otra capa:

- Las vistas de este proyecto escriben de formas distintas (ModelForm.save()
  en la mayoría, MovimientoSalida.objects.create() directo en
  generar_ajuste(), Producto.objects.get_or_create() en varios comandos de
  gestión) — no hay un único punto de entrada común más arriba, a nivel de
  vista, que ya cubra todos los casos.
- Una capa de "servicio de escritura" (ej. servicios.registrar_venta())
  sí sería un único punto de entrada, pero exigiría reescribir cada vista
  para llamarla en vez de guardar el form directamente — exactamente lo
  que este prompt pide evitar.
- Las señales post_save/post_delete de Django se disparan sin importar
  CÓMO se guardó o se borró el registro (form, .create(), admin, un
  comando de gestión, un shell) — es el punto más bajo y más completo
  para interceptar escrituras sin tocar código ya escrito.

Los modelos relevantes para sincronizar son LoteCompra, MovimientoSalida,
ConteoFisico y CorreccionHistorial (los 4 que representan hechos que
pasaron, no catálogo) — Categoria/Producto normalmente se editan en línea,
no offline, pero si el prompt 19 decide sincronizarlos también, el mismo
patrón aplica igual.

Cuando el prompt 19 implemente el motor real, lo natural es:
1. Escribir aquí receiver(s) para post_save/post_delete de esos modelos.
2. Conectarlos en InventarioConfig.ready() (ver apps.py) — nunca en
   models.py, para no crear una dependencia circular entre el módulo de
   modelos y el de sincronización.
3. Cada receiver encola el cambio (tabla local de "pendientes de subir",
   o similar) sin bloquear el guardado real — la escritura local nunca
   debe fallar ni esperar por la sincronización.
"""

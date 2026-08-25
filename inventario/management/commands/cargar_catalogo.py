from decimal import Decimal

from django.core.management.base import BaseCommand

from inventario.models import Categoria, Producto

# Catálogo inicial de bebidas embotelladas, extraído del sistema principal
# (productos con es_preparado=False en categorías claramente botella/lata
# cerrada). Ver decisiones del prompt 3:
#
# - "Corona" no existe como cerveza individual en el sistema principal
#   (solo aparece dentro de "Cubeta corona"); se crea aquí como producto
#   base nuevo en Cervezas con un precio placeholder (Q20, igual a Modelo
#   lata/Montecarlo) porque no se confirmó su precio real. AJUSTAR EN EL
#   ADMIN cuando se tenga el precio verdadero.
# - Los cubetazos son paquetes de 7 unidades del producto base (confirmado
#   por el usuario para las 6 cubetas), registrados vía producto_base /
#   unidades_por_paquete en Producto. El descuento automático del stock del
#   producto base al vender un cubetazo se implementa en el flujo de
#   registro de ventas, no en este comando.
# - "Cubeta normal gallo" y "Cubetazo gallo" se asumen ambos sobre la
#   botella "Gallo" (no la lata "Gallo lata"), por ser cubetas de botella.
# - entradas, frescos_naturales y micheladas_y_piconas quedan fuera: son
#   platillos/bebidas preparadas, no botellas cerradas.
# - No se incluyen licores ni vinos por botella completa (pendiente de
#   confirmar nombres y precios).

CERVEZAS = [
    ("Dorada draf", "20"),
    ("Gallo lata", "15"),
    ("Michelob ultra", "20"),
    ("Modelo lata", "20"),
    ("Tecate original", "15"),
    ("Bacardi silver", "20"),
    ("Gallo", "15"),
    ("Montecarlo", "20"),
    ("Stella artois", "25"),
    ("Corona", "20"),  # precio placeholder, no confirmado — ajustar en el admin
]

GASEOSAS = [
    ("Bote de agua", "10"),
    ("Coca cero", "10"),
    ("Coca lata", "10"),
    ("Fanta naranja", "10"),
    ("Fresca toronja", "10"),
    ("Pepsi black", "10"),
    ("Sprite", "10"),
    ("Coca cola", "10"),
]

UNIDADES_POR_CUBETAZO = 7

# (nombre del cubetazo, precio, nombre del producto base en Cervezas)
CUBETAZOS = [
    ("Cubeta dorada draft", "110", "Dorada draf"),
    ("Cubeta corona", "110", "Corona"),
    ("Cubeta monte carlo", "110", "Montecarlo"),
    ("Cubeta normal gallo", "90", "Gallo"),
    ("Cubeta tecate", "75", "Tecate original"),
    ("Cubetazo gallo", "75", "Gallo"),
]


class Command(BaseCommand):
    help = "Carga las categorías y productos iniciales de bebidas embotelladas (idempotente)."

    def handle(self, *args, **options):
        creados_categorias = 0
        creados_productos = 0

        cat_cervezas, created = Categoria.objects.get_or_create(nombre="Cervezas")
        creados_categorias += created
        cat_cubetazos, created = Categoria.objects.get_or_create(nombre="Cubetazos")
        creados_categorias += created
        cat_gaseosas, created = Categoria.objects.get_or_create(nombre="Gaseosas")
        creados_categorias += created

        productos_base = {}
        for nombre, precio in CERVEZAS:
            producto, created = Producto.objects.get_or_create(
                nombre=nombre,
                defaults={
                    "categoria": cat_cervezas,
                    "precio_venta_actual": Decimal(precio),
                    "activo": True,
                },
            )
            productos_base[nombre] = producto
            creados_productos += created

        for nombre, precio in GASEOSAS:
            _, created = Producto.objects.get_or_create(
                nombre=nombre,
                defaults={
                    "categoria": cat_gaseosas,
                    "precio_venta_actual": Decimal(precio),
                    "activo": True,
                },
            )
            creados_productos += created

        for nombre, precio, nombre_base in CUBETAZOS:
            _, created = Producto.objects.get_or_create(
                nombre=nombre,
                defaults={
                    "categoria": cat_cubetazos,
                    "precio_venta_actual": Decimal(precio),
                    "activo": True,
                    "producto_base": productos_base[nombre_base],
                    "unidades_por_paquete": UNIDADES_POR_CUBETAZO,
                },
            )
            creados_productos += created

        self.stdout.write(
            self.style.SUCCESS(
                f"Catálogo cargado. Categorías nuevas: {creados_categorias}. "
                f"Productos nuevos: {creados_productos}."
            )
        )

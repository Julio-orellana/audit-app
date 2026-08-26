# inventario/management/commands/vincular_productos_derivados.py
"""
Ajusta los datos existentes tras agregar la relación de equivalencia
producto_base / factor_equivalencia (prompt 15): confirma el factor de
"Cubeta corona" (el único cubetazo con el número real de botellas
confirmado por el usuario) y crea/enlaza "Sprite colaboradores" — la
variante de precio de Sprite cuya venta, antes de este prompt, se
descontaba de su propio stock en vez del de Sprite. Idempotente: se puede
correr más de una vez sin duplicar nada.

Los demás cubetazos (Cubeta dorada draft, Cubeta monte carlo, Cubeta
normal gallo, Cubeta tecate, Cubetazo gallo) ya quedaron enlazados a su
cerveza base por cargar_catalogo.py, con factor_equivalencia=1 como
placeholder — el auditor/administrador lo ajusta al valor real desde el
formulario de producto (no se adivina aquí).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from inventario.models import Producto


class Command(BaseCommand):
    help = "Confirma el factor de Cubeta corona (7) y enlaza/crea Sprite colaboradores -> Sprite."

    def handle(self, *args, **options):
        try:
            corona = Producto.objects.get(nombre="Cubeta corona")
        except Producto.DoesNotExist:
            raise CommandError("No existe 'Cubeta corona' — corre primero cargar_catalogo.")
        corona.factor_equivalencia = 7
        corona.full_clean()
        corona.save()
        self.stdout.write(self.style.SUCCESS("'Cubeta corona' confirmada con factor_equivalencia=7."))

        try:
            sprite = Producto.objects.get(nombre="Sprite")
        except Producto.DoesNotExist:
            raise CommandError("No existe 'Sprite' — no se puede enlazar 'Sprite colaboradores'.")

        derivado, creado = Producto.objects.get_or_create(
            nombre="Sprite colaboradores",
            defaults={
                "categoria": sprite.categoria,
                "precio_venta_actual": Decimal("6.00"),
                "activo": True,
                "producto_base": sprite,
                "factor_equivalencia": 1,
            },
        )
        if not creado:
            derivado.producto_base = sprite
            derivado.factor_equivalencia = 1
            derivado.full_clean()
            derivado.save()

        accion = "creado y enlazado" if creado else "ya existía — enlace confirmado"
        self.stdout.write(self.style.SUCCESS(f"'Sprite colaboradores' {accion} a 'Sprite' (factor_equivalencia=1)."))

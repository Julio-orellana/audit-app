# inventario/management/commands/limpiar_datos_prueba.py
"""
Elimina todos los movimientos de prueba (LoteCompra, MovimientoSalida,
ConteoFisico) para dejar la base de datos lista para que el auditor
empiece a usarla en real. Conserva intactos el catálogo (Categoria,
Producto) y los usuarios.

Confirmación obligatoria (prompt 32): este comando hace exactamente el
mismo borrado — sobre los mismos tres modelos — que el script que causó
el incidente del prompt 30 (una base de pruebas mal aislada terminó
apuntando a producción real, sin ninguna pregunta de por medio antes de
borrar). A diferencia de aquel script, ESTE comando SÍ está pensado para
correr contra producción — es su propósito real, una sola vez, al pasar
de datos de prueba a uso real — así que la corrección no es bloquearlo
fuera de producción, sino nunca dejarlo correr sin mostrar antes,
explícitamente, contra qué base de datos está a punto de escribir y sin
una confirmación real del operador. Ver seguridad_entorno_pruebas.py.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from inventario.models import (
    ConteoFisico, CorreccionHistorial, DiscrepanciaInventario, LoteCompra,
    MovimientoSalida, Producto,
)
from seguridad_entorno_pruebas import EntornoNoSeguroError, confirmar_operacion_riesgosa

# Las ÚNICAS tablas donde este comando puede borrar filas (prompt 31,
# lista blanca). El orden es el de borrado y respeta las claves foráneas:
# la discrepancia apunta al conteo y al ajuste, y el conteo apunta al
# ajuste. Verificado contra el esquema real: ninguna tabla de FUERA de
# esta lista tiene una clave foránea hacia estas, así que ningún borrado
# en cascada puede salirse de aquí.
LISTA_BLANCA = (
    ("DiscrepanciaInventario", DiscrepanciaInventario),
    ("ConteoFisico", ConteoFisico),
    ("MovimientoSalida", MovimientoSalida),
    ("LoteCompra", LoteCompra),
    ("CorreccionHistorial", CorreccionHistorial),
)

# Se cuentan antes y después para dejar constancia de que NO cambiaron.
# Nunca se escriben; están aquí solo como evidencia (prompt 31, punto 0).
LISTA_NEGRA_SQL = (
    "auth_user", "auth_group", "auth_permission", "auth_user_groups",
    "auth_group_permissions", "auth_user_user_permissions",
    "django_migrations", "django_content_type", "django_session",
    "django_admin_log", "inventario_categoria", "inventario_producto",
    "inventario_referenciaventaimportada",
)


def _conteo_lista_negra():
    """Filas de cada tabla intocable, para comparar antes/después."""
    from django.db import connection

    with connection.cursor() as cur:
        resultado = {}
        for tabla in LISTA_NEGRA_SQL:
            cur.execute(f'SELECT count(*) FROM "{tabla}"')
            resultado[tabla] = cur.fetchone()[0]
    return resultado


class Command(BaseCommand):
    help = (
        "Elimina las filas de las 5 tablas de datos transaccionales de prueba "
        "(DiscrepanciaInventario, ConteoFisico, MovimientoSalida, LoteCompra, "
        "CorreccionHistorial), conservando Categoria, Producto y los usuarios "
        "intactos. Con --dry-run solo cuenta. Pide confirmación explícita salvo "
        "que se pase --sin-confirmar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", dest="dry_run",
            help="Solo cuenta y lista lo que se borraría. No escribe absolutamente nada.",
        )
        parser.add_argument(
            "--sin-confirmar", action="store_true", dest="sin_confirmar",
            help="Omite la confirmación interactiva (uso en automatización ya controlada).",
        )

    def handle(self, *args, **options):
        from django.db import connection

        antes_blanca = {nombre: modelo.objects.count() for nombre, modelo in LISTA_BLANCA}
        antes_negra = _conteo_lista_negra()

        self.stdout.write("")
        self.stdout.write(f"  Base de datos: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  Host:          {connection.settings_dict.get('HOST')}")
        self.stdout.write("")
        self.stdout.write("  LISTA BLANCA — filas que se borrarían:")
        for nombre, _ in LISTA_BLANCA:
            self.stdout.write(f"    {nombre:26} {antes_blanca[nombre]:>5}")
        self.stdout.write(f"    {'TOTAL':26} {sum(antes_blanca.values()):>5}")
        self.stdout.write("")
        self.stdout.write("  LISTA NEGRA — no se tocan (se vuelven a contar al final):")
        for tabla, n in antes_negra.items():
            self.stdout.write(f"    {tabla:36} {n:>5}")
        self.stdout.write("")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "--dry-run: no se escribió NADA. Quita la bandera para aplicarlo."
            ))
            return

        try:
            confirmar_operacion_riesgosa(
                "borrar TODAS las filas de las 5 tablas de la lista blanca: "
                + ", ".join(f"{n} ({antes_blanca[n]})" for n, _ in LISTA_BLANCA)
                + f" — {sum(antes_blanca.values())} filas en total",
                forzar=options["sin_confirmar"],
            )
        except EntornoNoSeguroError as error:
            self.stderr.write(self.style.ERROR(str(error)))
            return

        # Todo dentro de UNA transacción: o se borra completo o no se borra
        # nada. Nunca DROP ni TRUNCATE — solo DELETE de filas, que es lo que
        # hace .delete() del ORM, sobre las cinco tablas de la lista blanca.
        borradas = {}
        with transaction.atomic():
            for nombre, modelo in LISTA_BLANCA:
                borradas[nombre] = modelo.objects.all().delete()[0]

        self.stdout.write(self.style.SUCCESS("Eliminados:"))
        for nombre, _ in LISTA_BLANCA:
            self.stdout.write(f"    {nombre:26} {borradas[nombre]:>5}")

        # Verificación: sin compras ni salidas, stock_teorico() debe dar 0 para
        # TODOS los productos de forma natural (es un cálculo, no un campo
        # aparte) — si algo no da 0, algo quedó sin borrar.
        productos = list(Producto.objects.all())
        con_stock_residual = [p for p in productos if p.stock_teorico() != 0]

        if con_stock_residual:
            detalle = ", ".join(f"{p.nombre} ({p.stock_teorico()})" for p in con_stock_residual)
            self.stderr.write(self.style.ERROR(
                f"ADVERTENCIA: {len(con_stock_residual)} producto(s) NO quedaron en stock 0: {detalle}"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Verificado: los {len(productos)} productos del catálogo quedaron con "
                f"stock_teorico() = 0."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Catálogo conservado sin cambios: {Producto.objects.count()} productos en "
            f"{Producto.objects.values('categoria').distinct().count()} categoría(s) con productos."
        ))

        # Evidencia final: ninguna tabla intocable cambió de tamaño.
        despues_negra = _conteo_lista_negra()
        cambiadas = {t: (antes_negra[t], despues_negra[t])
                     for t in LISTA_NEGRA_SQL if antes_negra[t] != despues_negra[t]}
        self.stdout.write("")
        self.stdout.write("  LISTA NEGRA — antes / después:")
        for tabla in LISTA_NEGRA_SQL:
            igual = "OK" if antes_negra[tabla] == despues_negra[tabla] else "*** CAMBIÓ ***"
            self.stdout.write(f"    {tabla:36} {antes_negra[tabla]:>5} -> {despues_negra[tabla]:>5}  {igual}")
        if cambiadas:
            self.stderr.write(self.style.ERROR(f"\nADVERTENCIA: cambiaron tablas intocables: {cambiadas}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nVerificado: ninguna tabla de la lista negra cambió su número de filas."
            ))

        despues_blanca = {nombre: modelo.objects.count() for nombre, modelo in LISTA_BLANCA}
        residuo = {n: v for n, v in despues_blanca.items() if v}
        if residuo:
            self.stderr.write(self.style.ERROR(f"ADVERTENCIA: quedaron filas sin borrar: {residuo}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Verificado: las cinco tablas de la lista blanca quedaron en 0 filas."
            ))

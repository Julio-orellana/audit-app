import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from inventario.models import ConteoFisico, LoteCompra, MovimientoSalida, Producto
from seguridad_entorno_pruebas import EntornoNoSeguroError, confirmar_operacion_riesgosa

# Mes de ejemplo: julio 2026, completamente en el pasado respecto a "hoy"
# (24/08/2026), útil para probar que un reporte de un mes cerrado no cambia.
FECHA_INICIO = date(2026, 7, 1)
FECHA_FIN = date(2026, 7, 31)

# Productos limpios (sin movimientos previos de otros prompts) para que este
# mes de prueba quede autocontenido y fácil de verificar a mano.
PRODUCTOS_PRUEBA = ["Modelo lata", "Michelob ultra", "Sprite", "Coca cola"]

PROVEEDORES = ["Distribuidora La Central", "Cervecería Nacional", "Comercial del Norte", None]

MOTIVOS_MERMA = [
    "Botella rota al acomodar el refrigerador",
    "Se derramó al servir, no se pudo vender",
    "Producto vencido, se dio de baja",
    "Rotura durante el descargue del proveedor",
]


class Command(BaseCommand):
    help = (
        "Genera un mes de datos de ejemplo (julio 2026) sobre el catálogo ya "
        "cargado, para verificar visualmente costo promedio, alertas de "
        "conteo físico y reportes Excel. No borra nada; seguro de re-ejecutar "
        "(cada producto se omite si ya tiene compras en julio 2026). Pide "
        "confirmación explícita salvo que se pase --sin-confirmar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sin-confirmar", action="store_true", dest="sin_confirmar",
            help="Omite la confirmación interactiva (uso en automatización ya controlada).",
        )

    def handle(self, *args, **options):
        # Confirmación obligatoria (prompt 32): esto genera movimientos
        # FALSOS (julio 2026, con datos aleatorios) — sembrarlos por
        # accidente en producción real, mezclados con movimientos
        # reales, sería casi tan difícil de deshacer como el borrado que
        # causó el incidente del prompt 30 (no queda una marca clara de
        # cuáles filas son de prueba y cuáles no). Ver seguridad_entorno_pruebas.py.
        try:
            confirmar_operacion_riesgosa(
                "sembrar un mes completo de movimientos DE PRUEBA (julio 2026, datos falsos)",
                forzar=options["sin_confirmar"],
            )
        except EntornoNoSeguroError as error:
            self.stderr.write(self.style.ERROR(str(error)))
            return

        random.seed(20260701)  # reproducible: mismos datos cada vez que se corre desde cero

        usuario = User.objects.filter(is_superuser=True).order_by("id").first()
        if usuario is None:
            self.stderr.write(self.style.ERROR("No hay ningún superusuario. Crea uno primero (createsuperuser)."))
            return

        productos = list(Producto.objects.filter(nombre__in=PRODUCTOS_PRUEBA))
        encontrados = {p.nombre for p in productos}
        faltantes = set(PRODUCTOS_PRUEBA) - encontrados
        if faltantes:
            self.stderr.write(
                self.style.ERROR(
                    f"Faltan productos en el catálogo: {', '.join(sorted(faltantes))}. "
                    "Corre 'python manage.py cargar_catalogo' primero."
                )
            )
            return

        productos_procesados = []
        for producto in productos:
            ya_tiene_datos = producto.lotes_compra.filter(
                fecha__gte=FECHA_INICIO, fecha__lte=FECHA_FIN
            ).exists()
            if ya_tiene_datos:
                self.stdout.write(
                    self.style.WARNING(f"{producto.nombre} ya tiene compras en julio 2026, se omite.")
                )
                continue
            productos_procesados.append(producto)

        if not productos_procesados:
            self.stdout.write(self.style.WARNING("Nada que generar: todos los productos ya tenían datos de julio 2026."))
            return

        total_lotes = 0
        total_ventas = 0
        for producto in productos_procesados:
            lotes, ventas = self._generar_compras_y_ventas(producto, usuario)
            total_lotes += lotes
            total_ventas += ventas

        # 2-3 mermas en total (no por producto), sobre productos que sí
        # tienen stock suficiente para soportarlas.
        total_mermas = 0
        num_mermas_objetivo = random.randint(2, 3)
        candidatos_merma = list(productos_procesados)
        random.shuffle(candidatos_merma)
        for producto in candidatos_merma:
            if total_mermas >= num_mermas_objetivo:
                break
            if self._generar_merma(producto, usuario):
                total_mermas += 1

        # 1-2 conteos físicos en total, con diferencia intencional, SIN
        # generar el ajuste (para que se vea la alerta pendiente en el
        # dashboard).
        total_conteos = 0
        num_conteos_objetivo = random.randint(1, 2)
        candidatos_conteo = list(productos_procesados)
        random.shuffle(candidatos_conteo)
        for producto in candidatos_conteo[:num_conteos_objetivo]:
            self._generar_conteo_con_diferencia(producto, usuario)
            total_conteos += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Datos de prueba de julio 2026 generados para {len(productos_procesados)} "
                f"producto(s): {total_lotes} lotes de compra, {total_ventas} ventas, "
                f"{total_mermas} mermas, {total_conteos} conteo(s) físico(s) con diferencia sin resolver."
            )
        )

    def _generar_compras_y_ventas(self, producto, usuario):
        """
        Genera 3-4 LoteCompra con costo ligeramente distinto entre sí, y
        ventas repartidas día a día durante julio. Las ventas se generan en
        orden cronológico y nunca superan el balance disponible hasta ese
        día (dejando siempre un colchón >=10 unidades), para que el stock
        teórico nunca sea negativo en ninguna fecha intermedia del mes.
        """
        costo_base = (producto.precio_venta_actual * Decimal("0.55")).quantize(Decimal("0.01"))
        if costo_base < Decimal("1.00"):
            costo_base = Decimal("1.00")

        num_lotes = random.randint(3, 4)
        dias_compra = sorted(random.sample(range(1, 22), num_lotes))  # primeras 3 semanas, deja margen a ventas

        eventos_compra = []
        for idx, dia in enumerate(dias_compra):
            cantidad = random.randint(120, 220)
            variacion = Decimal(str(round(random.uniform(-0.30, 0.55), 2)))
            costo_unitario = costo_base + variacion * idx
            if costo_unitario < Decimal("1.00"):
                costo_unitario = Decimal("1.00")
            eventos_compra.append({"dia": dia, "cantidad": cantidad, "costo": costo_unitario})

        lotes_creados = 0
        for evento in eventos_compra:
            fecha = FECHA_INICIO + timedelta(days=evento["dia"] - 1)
            LoteCompra.objects.create(
                producto=producto,
                fecha=fecha,
                cantidad=evento["cantidad"],
                costo_unitario=evento["costo"],
                proveedor=random.choice(PROVEEDORES),
                registrado_por=usuario,
            )
            lotes_creados += 1

        ventas_creadas = 0
        balance_disponible = 0
        idx_compra = 0
        for dia in range(1, 32):
            while idx_compra < len(eventos_compra) and eventos_compra[idx_compra]["dia"] <= dia:
                balance_disponible += eventos_compra[idx_compra]["cantidad"]
                idx_compra += 1

            if balance_disponible > 20 and random.random() < 0.5:
                cantidad_venta = min(balance_disponible - 10, random.randint(3, 12))
                if cantidad_venta > 0:
                    fecha = FECHA_INICIO + timedelta(days=dia - 1)
                    venta = MovimientoSalida(
                        producto=producto,
                        fecha=fecha,
                        tipo="venta",
                        cantidad=cantidad_venta,
                        registrado_por=usuario,
                    )
                    venta.save()  # auto-completa costo_unitario_snapshot y precio_venta_unitario
                    balance_disponible -= cantidad_venta
                    ventas_creadas += 1

        return lotes_creados, ventas_creadas

    def _generar_merma(self, producto, usuario):
        dia = random.randint(20, 28)
        fecha = FECHA_INICIO + timedelta(days=dia - 1)
        disponible = producto.stock_teorico(hasta_fecha=fecha)
        cantidad = min(random.randint(2, 5), max(disponible - 5, 0))
        if cantidad <= 0:
            return False
        MovimientoSalida.objects.create(
            producto=producto,
            fecha=fecha,
            tipo="merma",
            cantidad=cantidad,
            motivo=random.choice(MOTIVOS_MERMA),
            registrado_por=usuario,
        )
        return True

    def _generar_conteo_con_diferencia(self, producto, usuario):
        fecha = FECHA_FIN - timedelta(days=random.randint(0, 3))
        teorico = producto.stock_teorico(hasta_fecha=fecha)
        diferencia = random.choice([-8, -5, -3, 4, 6])
        cantidad_contada = max(teorico + diferencia, 0)
        ConteoFisico.objects.create(
            producto=producto,
            fecha=fecha,
            cantidad_contada=cantidad_contada,
            notas="Conteo de prueba (prompt 9) — diferencia intencional, ajuste NO generado a propósito.",
            registrado_por=usuario,
        )

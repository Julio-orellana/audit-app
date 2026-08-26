from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from .models import Categoria, ConteoFisico, LoteCompra, MovimientoSalida, Producto


class HistorialCompletoTests(TestCase):
    """
    Blinda que la vista de historial (inventario/urls.py -> "historial")
    siga mostrando, sin excepción, cada LoteCompra, cada MovimientoSalida
    (venta/merma/ajuste) y cada ConteoFisico — y que el filtro por producto
    y por rango de fechas siga funcionando exactamente.

    Si alguien rompe movimientos_periodo() (services.py) o HistorialView
    (views.py) de forma que deje de contar algún tipo de movimiento, estos
    tests deben fallar.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="auditor_test", password="clave-de-prueba-123")
        # Historial requiere rol admin/auditor desde el prompt 16 — sin
        # grupo, el usuario de prueba recibiría 403 en vez del 200 que
        # estos tests verifican.
        grupo_auditor, _ = Group.objects.get_or_create(name="auditor")
        self.user.groups.add(grupo_auditor)
        self.categoria = Categoria.objects.create(nombre="Categoría de prueba")
        self.producto = Producto.objects.create(
            nombre="Producto de prueba",
            categoria=self.categoria,
            precio_venta_actual=Decimal("20.00"),
        )
        self.client.login(username="auditor_test", password="clave-de-prueba-123")

    def test_historial_incluye_todos_los_tipos_de_movimiento_sin_filtro_de_fecha(self):
        fecha = date(2026, 3, 10)

        # 2 LoteCompra
        LoteCompra.objects.create(
            producto=self.producto, fecha=fecha, cantidad=100,
            costo_unitario=Decimal("10.00"), registrado_por=self.user,
        )
        LoteCompra.objects.create(
            producto=self.producto, fecha=fecha, cantidad=50,
            costo_unitario=Decimal("11.00"), registrado_por=self.user,
        )

        # 2 MovimientoSalida tipo venta
        MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha, tipo="venta", cantidad=10, registrado_por=self.user,
        )
        MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha, tipo="venta", cantidad=5, registrado_por=self.user,
        )

        # 1 MovimientoSalida tipo merma
        MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha, tipo="merma", cantidad=2,
            motivo="Botella rota (prueba)", registrado_por=self.user,
        )

        # 1 ConteoFisico que NO genera ajuste (queda pendiente, tal como
        # exige el diseño del prompt 11a: nunca se resuelve solo).
        ConteoFisico.objects.create(
            producto=self.producto, fecha=fecha, cantidad_contada=130, registrado_por=self.user,
        )

        # stock_teorico en este punto: 150 comprado - 15 vendido - 2 merma = 133
        # 1 ConteoFisico que SI genera su ajuste correspondiente (esto crea
        # un MovimientoSalida tipo "ajuste" adicional, igual que el botón
        # "Generar ajuste" real).
        conteo_con_ajuste = ConteoFisico.objects.create(
            producto=self.producto, fecha=fecha, cantidad_contada=140, registrado_por=self.user,
        )
        diferencia = conteo_con_ajuste.diferencia
        self.assertNotEqual(diferencia, 0, "el conteo de prueba debe tener una diferencia real para generar ajuste")
        ajuste = MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha, tipo="ajuste", cantidad=-diferencia,
            motivo="Ajuste generado en prueba automatizada", registrado_por=self.user,
        )
        conteo_con_ajuste.ajuste_generado = ajuste
        conteo_con_ajuste.save()

        # Total esperado en el historial:
        #   2 LoteCompra (Entrada)
        # + 2 MovimientoSalida venta + 1 merma + 1 ajuste = 4 MovimientoSalida
        # + 2 ConteoFisico (se muestran ambos, resuelto o no)
        # = 8
        total_esperado = 2 + (2 + 1 + 1) + 2
        self.assertEqual(total_esperado, 8)

        response = self.client.get(reverse("historial"), {"producto": self.producto.pk})
        self.assertEqual(response.status_code, 200)

        filas = response.context["filas"]
        self.assertEqual(len(filas), total_esperado)

        # No solo la cantidad total: verifica que cada TIPO esté representado
        # correctamente, para no dejar pasar un bug donde el total cuadra
        # por casualidad pero algún tipo de movimiento falta y otro sobra.
        tipos = [fila["tipo"] for fila in filas]
        self.assertEqual(tipos.count("Entrada"), 2)
        self.assertEqual(tipos.count("Venta"), 2)
        self.assertEqual(tipos.count("Merma"), 1)
        self.assertEqual(tipos.count("Conteo físico"), 2)
        self.assertEqual(sum(1 for t in tipos if "Ajuste" in t), 1)

    def test_historial_filtra_correctamente_por_rango_de_fechas(self):
        fecha_marzo = date(2026, 3, 10)
        fecha_abril = date(2026, 4, 15)

        # 3 movimientos en marzo
        LoteCompra.objects.create(
            producto=self.producto, fecha=fecha_marzo, cantidad=100,
            costo_unitario=Decimal("10.00"), registrado_por=self.user,
        )
        MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha_marzo, tipo="venta", cantidad=10, registrado_por=self.user,
        )
        ConteoFisico.objects.create(
            producto=self.producto, fecha=fecha_marzo, cantidad_contada=90, registrado_por=self.user,
        )

        # 3 movimientos en abril (deben quedar excluidos del filtro)
        LoteCompra.objects.create(
            producto=self.producto, fecha=fecha_abril, cantidad=80,
            costo_unitario=Decimal("12.00"), registrado_por=self.user,
        )
        MovimientoSalida.objects.create(
            producto=self.producto, fecha=fecha_abril, tipo="venta", cantidad=20, registrado_por=self.user,
        )
        ConteoFisico.objects.create(
            producto=self.producto, fecha=fecha_abril, cantidad_contada=150, registrado_por=self.user,
        )

        response = self.client.get(
            reverse("historial"),
            {
                "producto": self.producto.pk,
                "fecha_desde": "2026-03-01",
                "fecha_hasta": "2026-03-31",
            },
        )
        self.assertEqual(response.status_code, 200)

        filas = response.context["filas"]
        self.assertEqual(len(filas), 3)
        for fila in filas:
            self.assertEqual(fila["fecha"], fecha_marzo)

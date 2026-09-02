from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from .forms import ConteoFisicoForm, LoteCompraForm, MovimientoSalidaForm
from .discrepancias import resolver_discrepancia
from .models import (
    Categoria, ConteoFisico, DiscrepanciaInventario, LoteCompra, MovimientoSalida, Producto,
)


def _deserializar_valor_prueba(valor):
    from .offline import _deserializar_valor

    return _deserializar_valor(valor)


def _es_postgres(engine):
    """
    ¿Este ENGINE habla Postgres?

    No basta con comparar contra "django.db.backends.postgresql": desde
    el prompt 33c la app usa un backend propio
    ("inventario.db_backend") que es ese mismo backend más un corto para
    no reintentar la red cuando ya consta que está caída. Comparar por
    igualdad hacía que TRES pruebas específicas de Postgres se saltaran
    siempre, en silencio y también contra Neon — o sea que dejaban de
    proteger nada sin que nadie se enterara.
    """
    return engine in ("django.db.backends.postgresql", "inventario.db_backend")


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

    # "local_disco" (prompt 19): cada request autenticada de verdad (via
    # self.client) toca la base local — el contador de pendientes del
    # navbar, la caché de credenciales al iniciar sesión. TestCase aísla
    # por alias a propósito, hay que declararlo o Django lo bloquea.
    databases = {"default", "local_disco"}

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


class ProductosDerivadosTests(TestCase):
    """
    Blinda, con aserciones exactas, lo que el prompt 28 confirmó a mano:
    el recálculo de stock_teorico() del producto base es correcto al
    editar/eliminar movimientos de un producto derivado — incluyendo
    cambiar entre dos derivados con factor_equivalencia distinto y
    ediciones encadenadas — y las restricciones del prompt 28b:
    ConteoFisico/LoteCompra no se pueden registrar sobre un derivado, y
    el selector de producto de sus formularios no lo ofrece como opción
    (MovimientoSalidaForm sí debe seguir ofreciéndolo).
    """

    databases = {"default", "local_disco"}  # ver el mismo comentario en HistorialCompletoTests

    def setUp(self):
        self.admin = User.objects.create_user(username="admin_test", password="clave-de-prueba-123")
        grupo_admin, _ = Group.objects.get_or_create(name="admin")
        self.admin.groups.add(grupo_admin)
        self.client.login(username="admin_test", password="clave-de-prueba-123")

        self.categoria = Categoria.objects.create(nombre="Categoría de prueba")
        self.base = Producto.objects.create(
            nombre="Base de prueba",
            categoria=self.categoria,
            precio_venta_actual=Decimal("15.00"),
        )
        self.derivado_a = Producto.objects.create(
            nombre="Derivado A de prueba",
            categoria=self.categoria,
            precio_venta_actual=Decimal("90.00"),
            producto_base=self.base,
            factor_equivalencia=6,
        )
        self.derivado_b = Producto.objects.create(
            nombre="Derivado B de prueba",
            categoria=self.categoria,
            precio_venta_actual=Decimal("45.00"),
            producto_base=self.base,
            factor_equivalencia=3,
        )
        self.fecha = date(2026, 3, 10)

    def _editar(self, mov, **cambios):
        """POST al formulario de corrección de un MovimientoSalida ya guardado."""
        datos = {
            "producto": mov.producto_id,
            "fecha": mov.fecha,
            "tipo": mov.tipo,
            "cantidad": mov.cantidad,
            "motivo": "Corrección de prueba automatizada",
        }
        datos.update(cambios)
        return self.client.post(
            reverse("movimientosalida_correccion_editar", args=[mov.pk]), datos
        )

    def test_editar_cantidad_de_movimiento_derivado_recalcula_stock_del_base(self):
        mov = MovimientoSalida.objects.create(
            producto=self.derivado_a, fecha=self.fecha, tipo="venta", cantidad=2,
            registrado_por=self.admin,
        )
        self.assertEqual(self.base.stock_teorico(), -2 * 6)

        response = self._editar(mov, cantidad=5)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.base.stock_teorico(), -5 * 6)

    def test_eliminar_movimiento_derivado_recalcula_stock_del_base(self):
        mov = MovimientoSalida.objects.create(
            producto=self.derivado_a, fecha=self.fecha, tipo="venta", cantidad=4,
            registrado_por=self.admin,
        )
        self.assertEqual(self.base.stock_teorico(), -4 * 6)

        response = self.client.post(
            reverse("movimientosalida_correccion_eliminar", args=[mov.pk]),
            {"motivo_correccion": "Eliminación de prueba automatizada"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MovimientoSalida.objects.filter(pk=mov.pk).exists())
        self.assertEqual(self.base.stock_teorico(), 0)

    def test_editar_entre_dos_derivados_con_factor_distinto_recalcula_correctamente(self):
        mov = MovimientoSalida.objects.create(
            producto=self.derivado_a, fecha=self.fecha, tipo="venta", cantidad=2,
            registrado_por=self.admin,
        )
        self.assertEqual(self.base.stock_teorico(), -2 * 6)  # -12

        response = self._editar(mov, producto=self.derivado_b.pk, cantidad=2)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.base.stock_teorico(), -2 * 3)  # -6, no -12

        # El precio/costo del movimiento también debe reflejar el nuevo
        # producto (prompt 28), no quedarse pegado al anterior.
        mov.refresh_from_db()
        self.assertEqual(mov.producto_id, self.derivado_b.pk)
        self.assertEqual(mov.precio_venta_unitario, Decimal("45.00"))

    def test_ediciones_encadenadas_siempre_reflejan_el_estado_actual(self):
        mov = MovimientoSalida.objects.create(
            producto=self.derivado_a, fecha=self.fecha, tipo="venta", cantidad=1,
            registrado_por=self.admin,
        )
        self.assertEqual(self.base.stock_teorico(), -1 * 6)  # -6

        self._editar(mov, producto=self.derivado_b.pk, cantidad=1)
        self.assertEqual(self.base.stock_teorico(), -1 * 3)  # -3, no -6 ni -9

        self._editar(mov, producto=self.derivado_a.pk, cantidad=2)
        self.assertEqual(self.base.stock_teorico(), -2 * 6)  # -12, no -3 ni -6

        # Cambiar al producto BASE directamente (no un derivado): ahora
        # afecta el stock de forma directa, no vía la suma de derivados —
        # el resultado tras el tercer cambio debe reflejar SOLO este
        # último estado.
        self._editar(mov, producto=self.base.pk, cantidad=3)
        self.assertEqual(self.base.stock_teorico(), -3)  # -3, no -12 ni ningún intermedio

    def test_lotecompra_no_se_puede_crear_para_producto_derivado(self):
        lote = LoteCompra(
            producto=self.derivado_a, fecha=self.fecha, cantidad=10,
            costo_unitario=Decimal("5.00"),
        )
        with self.assertRaises(ValidationError) as contexto:
            lote.full_clean()
        self.assertIn("producto", contexto.exception.message_dict)

    def test_conteofisico_no_se_puede_crear_para_producto_derivado(self):
        conteo = ConteoFisico(producto=self.derivado_a, fecha=self.fecha, cantidad_contada=5)
        with self.assertRaises(ValidationError) as contexto:
            conteo.full_clean()
        self.assertIn("producto", contexto.exception.message_dict)
        mensaje = contexto.exception.message_dict["producto"][0]
        self.assertIn(self.derivado_a.nombre, mensaje)
        self.assertIn(self.base.nombre, mensaje)

    def test_selector_de_producto_excluye_derivados_en_entradas_y_conteos_pero_no_en_salidas(self):
        queryset_lotes = LoteCompraForm().fields["producto"].queryset
        self.assertIn(self.base, queryset_lotes)
        self.assertNotIn(self.derivado_a, queryset_lotes)
        self.assertNotIn(self.derivado_b, queryset_lotes)

        queryset_conteos = ConteoFisicoForm().fields["producto"].queryset
        self.assertIn(self.base, queryset_conteos)
        self.assertNotIn(self.derivado_a, queryset_conteos)
        self.assertNotIn(self.derivado_b, queryset_conteos)

        queryset_salidas = MovimientoSalidaForm().fields["producto"].queryset
        self.assertIn(self.base, queryset_salidas)
        self.assertIn(self.derivado_a, queryset_salidas)
        self.assertIn(self.derivado_b, queryset_salidas)


# ---------------------------------------------------------------------------
# Prompt 19b — motor de sincronización offline: correcciones críticas
# ---------------------------------------------------------------------------

class ClasificacionOfflineTests(SimpleTestCase):
    """
    Prompt 19b, punto 4: "haz un barrido de todas las vistas de la app y
    clasifícalas explícitamente... no dejes ninguna vista sin clasificar".

    Esta prueba convierte esa exigencia en algo que la máquina verifica
    sola: recorre TODAS las rutas del proyecto y exige que cada vista
    declare exactamente una de las dos marcas (`funciona_sin_conexion` o
    `requiere_conexion_activa`). Una vista nueva que nadie clasifique
    hace fallar la suite en vez de descubrirse el día del apagón — que
    es exactamente como se descubrió este bug.
    """

    # /admin/ de Django es de terceros y no se puede marcar desde aquí:
    # queda cubierto por ManejoGlobalErrorConexionMiddleware, que atrapa
    # el error de conexión de cualquier vista y muestra la pantalla clara.
    PREFIJOS_EXENTOS = ("admin/",)

    def _rutas(self, resolver, prefijo=""):
        for patron in resolver.url_patterns:
            ruta = prefijo + str(patron.pattern)
            if hasattr(patron, "url_patterns"):
                yield from self._rutas(patron, ruta)
            else:
                yield ruta, patron.callback

    def test_toda_vista_esta_clasificada_como_offline_o_requiere_conexion(self):
        from django.urls import get_resolver

        sin_clasificar = []
        ambiguas = []
        for ruta, callback in self._rutas(get_resolver()):
            if ruta.startswith(self.PREFIJOS_EXENTOS):
                continue
            objetivo = getattr(callback, "view_class", callback)
            offline = getattr(objetivo, "funciona_sin_conexion", False) or getattr(
                callback, "funciona_sin_conexion", False
            )
            online = getattr(objetivo, "requiere_conexion_activa", False) or getattr(
                callback, "requiere_conexion_activa", False
            )
            if offline and online:
                ambiguas.append(ruta)
            elif not offline and not online:
                sin_clasificar.append(ruta)

        self.assertEqual(
            sin_clasificar, [],
            "Estas vistas no están clasificadas para el modo sin conexión. Agrega "
            "RequiereConexionMixin/@requiere_conexion (si necesitan internet) o "
            "funciona_sin_conexion=True/@funciona_sin_conexion (si no) — ver el "
            "docstring de inventario/resiliencia.py.",
        )
        self.assertEqual(ambiguas, [], "Estas vistas están marcadas como offline Y como online a la vez.")

    def test_las_cuatro_escrituras_offline_estan_del_lado_correcto(self):
        from .views import (
            ConteoFisicoCreateView,
            LoteCompraCreateView,
            MovimientoSalidaCreateView,
        )

        for vista in (LoteCompraCreateView, MovimientoSalidaCreateView, ConteoFisicoCreateView):
            self.assertTrue(vista.funciona_sin_conexion, vista.__name__)
            self.assertFalse(getattr(vista, "requiere_conexion_activa", False), vista.__name__)

    def test_editar_y_eliminar_historial_nunca_funcionan_sin_conexion(self):
        """
        Regla explícita del prompt 17/19: editar o eliminar historial
        SIEMPRE exige conexión y NUNCA se encola, bajo ninguna
        circunstancia.
        """
        from .offline import ColaOfflineMixin
        from .views import (
            ConteoFisicoCorreccionDeleteView,
            ConteoFisicoCorreccionUpdateView,
            LoteCompraCorreccionDeleteView,
            LoteCompraCorreccionUpdateView,
            MovimientoSalidaCorreccionDeleteView,
            MovimientoSalidaCorreccionUpdateView,
        )

        for vista in (
            LoteCompraCorreccionUpdateView, MovimientoSalidaCorreccionUpdateView,
            ConteoFisicoCorreccionUpdateView, LoteCompraCorreccionDeleteView,
            MovimientoSalidaCorreccionDeleteView, ConteoFisicoCorreccionDeleteView,
        ):
            self.assertTrue(vista.requiere_conexion_activa, vista.__name__)
            self.assertFalse(getattr(vista, "funciona_sin_conexion", False), vista.__name__)
            self.assertNotIn(ColaOfflineMixin, vista.__mro__, vista.__name__)


class ConectividadTests(SimpleTestCase):
    """
    Blinda el bug raíz del prompt 19b, punto 2: hay_conexion() daba un
    FALSO POSITIVO permanente cuando la conexión persistente moría (la
    red se cayó), porque ensure_connection() de Django solo conecta si
    `self.connection is None` y no revisa una conexión ya existente pero
    muerta. Con eso, el hilo de sincronización quedaba roto para siempre
    tras el primer corte de red — para los tres roles por igual.

    SimpleTestCase (no TestCase) a propósito: estos tests matan la
    conexión a mano, algo incompatible con la transacción en la que
    TestCase envuelve cada prueba.
    """

    databases = {"default"}

    def setUp(self):
        # Estas pruebas solo tienen sentido contra Postgres: simulan un
        # corte tumbando el socket o apuntando HOST/PORT a un puerto
        # cerrado, y SQLite ignora ambas cosas (es un archivo local, no
        # tiene socket ni host). Corriendo la suite contra SQLite —
        # documentado en DESARROLLO.md como forma válida de trabajar sin
        # nube— fallaban con "hay_conexion() reportó conexión donde no la
        # hay", que parece una regresión del motor offline y no lo es.
        # Se salta explícitamente para que ese falso positivo no vuelva a
        # costar tiempo.
        from django.db import connections

        if not _es_postgres(connections["default"].settings_dict["ENGINE"]):
            self.skipTest(
                "Solo aplica contra Postgres/Neon: SQLite no tiene socket ni host que tumbar."
            )

        # La caché de "sin conexión" (prompt 33) es estado GLOBAL del
        # proceso: estos tests tumban la conexión a propósito, así que sin
        # reiniciarla, el siguiente test arrancaría con "sin conexión"
        # pegado hasta 15s y fallaría por un motivo ajeno a lo que prueba.
        from .offline import reiniciar_cache_conexion

        reiniciar_cache_conexion()
        self.addCleanup(reiniciar_cache_conexion)

    def test_una_conexion_muerta_se_descarta_y_la_siguiente_consulta_funciona(self):
        from django.db import connections

        from .offline import hay_conexion

        conexion = connections["default"]
        self.assertTrue(hay_conexion(), "La prueba necesita una conexión sana de partida.")

        # Mata el socket por debajo sin que Django se entere — es
        # exactamente lo que pasa cuando se cae la red con una conexión
        # persistente abierta.
        conexion.connection.close()
        self.assertTrue(conexion.connection.closed)

        self.assertTrue(hay_conexion())
        # ANTES DEL ARREGLO esta consulta reventaba con OperationalError
        # ("the connection is closed"), ciclo tras ciclo y para siempre.
        self.assertIsInstance(MovimientoSalida.objects.count(), int)

    def test_sin_conexion_real_devuelve_false_y_no_un_falso_positivo(self):
        """
        Base inalcanzable (sin mocks): se apunta la conexión a un puerto
        local cerrado.

        OJO con el alcance de esta prueba (prompt 33): un puerto cerrado
        RECHAZA al instante, que NO es lo mismo que un corte de red real,
        donde los paquetes se pierden en silencio y hay que esperar el
        timeout. Medido: 0.00s contra 75s. Esta prueba solo verifica que
        no haya un falso positivo; que además falle RÁPIDO ante un corte
        real depende de connect_timeout (settings.py) y de la caché de
        resultado negativo, no de esto.
        """
        from django.db import connections

        from .offline import hay_conexion, reiniciar_cache_conexion

        conexion = connections["default"]
        hay_conexion()
        original = dict(conexion.settings_dict)
        conexion.close()
        conexion.settings_dict["HOST"] = "127.0.0.1"
        conexion.settings_dict["PORT"] = "1"  # puerto cerrado a propósito
        conexion.settings_dict["OPTIONS"] = {"connect_timeout": 2}
        try:
            self.assertFalse(hay_conexion(), "hay_conexion() reportó conexión donde no la hay.")
        finally:
            conexion.close()
            conexion.settings_dict.clear()
            conexion.settings_dict.update(original)
        # El assertFalse de arriba dejó cacheado "sin conexión" por
        # SEGUNDOS_CACHE_SIN_CONEXION (prompt 33) — es el comportamiento
        # deseado en producción (evita pagar el timeout en cada request),
        # y significa que al volver la conexión la app tarda hasta 15s en
        # notarlo. Aquí se reinicia para verificar la reconexión en sí,
        # que es lo que esta prueba mide.
        reiniciar_cache_conexion()
        self.assertTrue(hay_conexion())

    def test_ninguna_consulta_reintenta_la_red_cuando_ya_consta_que_no_hay(self):
        """
        Prompt 33c: el corto tiene que estar en la CAPA DE CONEXIÓN, no
        en cada vista.

        Arreglar get_user() bajó el costo de muchas páginas, pero no de
        todas: cualquier consulta directa al ORM abría su propio intento
        y pagaba el timeout entero sin enterarse de que otro hilo ya
        había comprobado que no hay red. Medido en la VM de Windows, con
        connect_timeout=3 y un host que resuelve a 3 direcciones (9.2s
        por intento):

            28506ms  GET 503  /correcciones/
            27660ms  GET 503  /salidas/176/editar/
            19450ms  POST 302 /login/

        O sea hasta TRES intentos dentro de una sola request. Esta
        prueba fija que, con la caché marcada, una consulta cualquiera
        falle al instante en vez de salir a la red.
        """
        import time

        from django.db import connections
        from django.db.utils import OperationalError

        from .offline import marcar_sin_conexion

        conexion = connections["default"]
        conexion.close()
        self.addCleanup(conexion.close)
        marcar_sin_conexion()
        inicio = time.monotonic()
        with self.assertRaises(OperationalError):
            conexion.ensure_connection()
        duracion = time.monotonic() - inicio

        self.assertLess(
            duracion, 0.5,
            f"El intento tardó {duracion:.2f}s: se salió a la red aunque ya constaba que "
            "no hay conexión. Eso es lo que dejaba páginas de 28 segundos.",
        )

    def test_dentro_de_una_transaccion_no_se_toca_la_conexion(self):
        """
        hay_conexion() se llama desde dentro de transaction.atomic() en
        varios caminos reales (correcciones de historial, generar_ajuste).
        Si ahí cerrara la conexión para "reciclarla", abortaría la
        transacción en curso y se perdería lo que llevaba hecha.
        """
        from django.db import connections, transaction

        from .offline import hay_conexion

        conexion = connections["default"]
        with transaction.atomic():
            objeto_antes = conexion.connection
            self.assertTrue(hay_conexion())
            self.assertIs(conexion.connection, objeto_antes, "hay_conexion() recicló la conexión dentro de una transacción.")
            self.assertIsInstance(MovimientoSalida.objects.count(), int)


class AutenticacionOfflineTests(TestCase):
    """
    Prompt 19b, punto 1: iniciar sesión sin conexión, contra la caché
    local de credenciales, para los tres roles.
    """

    databases = {"default", "local_disco"}

    CLAVE = "clave-de-prueba-19b"

    def setUp(self):
        from .models import CredencialOfflineCache

        CredencialOfflineCache.objects.using("local_disco").all().delete()
        self.usuarios = {}
        for rol in ("admin", "auditor", "vendedor"):
            user = User.objects.create_user(username=f"{rol}_test", password=self.CLAVE)
            grupo, _ = Group.objects.get_or_create(name=rol)
            user.groups.add(grupo)
            self.usuarios[rol] = user

    def _cachear(self, rol):
        from .offline import guardar_credencial_offline

        guardar_credencial_offline(self.usuarios[rol], rol)

    def test_get_user_no_toca_la_nube_cuando_ya_consta_que_esta_caida(self):
        """
        Prompt 33c, punto 3 ("la ventana nunca queda insensible").

        get_user() resuelve request.user en CADA request de un usuario
        con sesión iniciada, así que es el camino más transitado de la
        app. Antes intentaba siempre la nube: sin conexión eso costaba
        el connect_timeout ENTERO (3.04s medidos) en todas y cada una de
        las páginas, incluidas las que ni siquiera consultan la nube —
        un 403 por rol, /instrucciones/, el sondeo del tablero cada 4s.
        Ese era el "no responde" que se reportó en Windows.

        Se verifica con assertNumQueries(0) sobre "default", que es el
        blindaje directo: cero consultas a la nube cuando ya consta que
        está caída.
        """
        from .offline import (
            BackendConRespaldoOffline,
            marcar_sin_conexion,
            reiniciar_cache_conexion,
        )

        self._cachear("auditor")
        self.addCleanup(reiniciar_cache_conexion)
        marcar_sin_conexion()

        backend = BackendConRespaldoOffline()
        with self.assertNumQueries(0, using="default"):
            usuario = backend.get_user(self.usuarios["auditor"].pk)

        self.assertIsNotNone(usuario, "Debe resolverse desde la caché local, no quedar anónimo.")
        self.assertEqual(usuario.username, "auditor_test")

    def test_get_user_no_agrega_un_viaje_a_la_nube_cuando_si_hay_conexion(self):
        """
        La otra mitad del mismo arreglo, y la razón de que se consulte
        sin_conexion_reciente() (que solo LEE la caché) en vez de
        hay_conexion() (que sondea). Medido contra Neon: un sondeo con la
        conexión sana cuesta ~87ms, o sea un viaje de ida y vuelta por
        red; ponerlo en get_user() se lo sumaría a TODAS las requests del
        uso normal CON internet, para beneficiar solo al caso sin
        conexión. Esta prueba fija que el camino online siga costando
        exactamente una consulta — la de siempre, la que busca al
        usuario — y ni una más.
        """
        from .offline import BackendConRespaldoOffline, reiniciar_cache_conexion

        reiniciar_cache_conexion()  # nada cacheado: es el caso "con conexión"
        backend = BackendConRespaldoOffline()
        with self.assertNumQueries(1, using="default"):
            usuario = backend.get_user(self.usuarios["admin"].pk)
        self.assertEqual(usuario.username, "admin_test")

    def test_nunca_se_guarda_la_contrasena_en_texto_plano(self):
        from .models import CredencialOfflineCache

        self._cachear("admin")
        fila = CredencialOfflineCache.objects.using("local_disco").get(username="admin_test")
        self.assertNotIn(self.CLAVE, fila.password_hash)
        self.assertTrue(fila.password_hash.startswith("pbkdf2_"), fila.password_hash[:20])

    def test_los_tres_roles_pueden_autenticarse_sin_conexion(self):
        from .offline import autenticar_offline

        for rol in ("admin", "auditor", "vendedor"):
            self._cachear(rol)
            user = autenticar_offline(f"{rol}_test", self.CLAVE)
            self.assertIsNotNone(user, f"{rol} no pudo autenticarse sin conexión")
            self.assertEqual(user.username, f"{rol}_test")
            self.assertEqual(user._rol_cache, rol)
            self.assertTrue(user.is_authenticated)

    def test_contrasena_incorrecta_se_rechaza_igual_sin_conexion(self):
        from .offline import autenticar_offline

        self._cachear("admin")
        self.assertIsNone(autenticar_offline("admin_test", "otra-clave-cualquiera"))

    def test_usuario_que_nunca_entro_en_esta_maquina_no_puede_entrar_sin_conexion(self):
        from .offline import autenticar_offline

        self.assertIsNone(autenticar_offline("auditor_test", self.CLAVE))

    def test_usuario_desactivado_no_puede_entrar_sin_conexion(self):
        from .offline import autenticar_offline

        self._cachear("vendedor")
        self.usuarios["vendedor"].is_active = False
        self.usuarios["vendedor"].save()
        self._cachear("vendedor")
        self.assertIsNone(autenticar_offline("vendedor_test", self.CLAVE))

    def test_el_rol_cacheado_deja_pasar_los_permisos_sin_consultar_grupos(self):
        """
        Sin conexión no se puede consultar auth_user_groups — el rol
        tiene que salir de la caché, o RequiereRol tumbaría la request
        antes de llegar a la cola offline.
        """
        from .offline import autenticar_offline
        from .permisos import _tiene_rol

        self._cachear("auditor")
        user = autenticar_offline("auditor_test", self.CLAVE)
        self.assertTrue(_tiene_rol(user, ("admin", "auditor")))
        self.assertFalse(_tiene_rol(user, ("vendedor",)))

    def test_revalidacion_detecta_una_contrasena_cambiada(self):
        from .models import CredencialOfflineCache
        from .offline import autenticar_offline, refrescar_credenciales_cache

        self._cachear("admin")
        self.usuarios["admin"].set_password("clave-nueva-cambiada-999")
        self.usuarios["admin"].save()

        refrescar_credenciales_cache()

        fila = CredencialOfflineCache.objects.using("local_disco").get(username="admin_test")
        self.assertTrue(fila.aviso_password_cambiada)
        # La caché quedó al día: la vieja ya no sirve, la nueva sí.
        self.assertIsNone(autenticar_offline("admin_test", self.CLAVE))
        self.assertIsNotNone(autenticar_offline("admin_test", "clave-nueva-cambiada-999"))

    def test_revalidacion_borra_la_credencial_de_un_usuario_eliminado(self):
        from .models import CredencialOfflineCache
        from .offline import refrescar_credenciales_cache

        self._cachear("vendedor")
        self.usuarios["vendedor"].delete()
        refrescar_credenciales_cache()
        self.assertFalse(
            CredencialOfflineCache.objects.using("local_disco").filter(username="vendedor_test").exists()
        )

    def test_iniciar_sesion_con_conexion_cachea_la_credencial_sola(self):
        from .models import CredencialOfflineCache

        self.assertTrue(self.client.login(username="admin_test", password=self.CLAVE))
        self.assertTrue(
            CredencialOfflineCache.objects.using("local_disco").filter(username="admin_test").exists(),
            "Un inicio de sesión normal debe dejar lista la credencial para el próximo apagón.",
        )


class ColaOfflineTests(TestCase):
    """
    Prompt 19b, puntos 2 y 3: la cola persiste en archivo para los TRES
    roles y la sincronización es real e idempotente.
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        from .models import PendienteSincronizacion

        PendienteSincronizacion.objects.using("local_disco").all().delete()
        self.user = User.objects.create_user(username="vendedor_cola", password="clave-de-prueba-19b")
        grupo, _ = Group.objects.get_or_create(name="vendedor")
        self.user.groups.add(grupo)
        self.categoria = Categoria.objects.create(nombre="Categoría cola")
        self.producto = Producto.objects.create(
            nombre="Producto cola", categoria=self.categoria, precio_venta_actual=Decimal("25.00")
        )
        LoteCompra.objects.create(
            producto=self.producto, fecha=date(2026, 8, 1), cantidad=100,
            costo_unitario=Decimal("10.00"), registrado_por=self.user,
        )

    def test_la_cola_es_la_misma_para_los_tres_roles(self):
        """
        Ya no existe el caso especial "vendedor en memoria" (prompt 19b,
        punto 3): un solo alias, en archivo, para todos.
        """
        from django.conf import settings

        from .offline import ALIAS_LOCAL

        self.assertEqual(ALIAS_LOCAL, "local_disco")
        self.assertNotIn("local_memoria", settings.DATABASES)
        self.assertEqual(
            settings.DATABASES["local_disco"]["ENGINE"], "django.db.backends.sqlite3"
        )

    def test_encolar_y_sincronizar_crea_el_movimiento_en_la_base_real(self):
        from .models import PendienteSincronizacion
        from .offline import encolar_pendiente, sincronizar_pendientes

        movimiento = MovimientoSalida(
            producto=self.producto, fecha=date(2026, 8, 27), tipo="venta", cantidad=3,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("25.00"),
            registrado_por_id=self.user.pk,
        )
        encolar_pendiente(movimiento)
        self.assertEqual(PendienteSincronizacion.objects.using("local_disco").count(), 1)

        self.assertEqual(sincronizar_pendientes(), 1)

        creado = MovimientoSalida.objects.get(uuid=movimiento.uuid)
        self.assertEqual(creado.cantidad, 3)
        self.assertEqual(creado.producto_id, self.producto.pk)
        self.assertEqual(creado.registrado_por_id, self.user.pk)
        # La fecha es la de creación LOCAL, nunca la de sincronización.
        self.assertEqual(creado.fecha, date(2026, 8, 27))
        self.assertEqual(PendienteSincronizacion.objects.using("local_disco").count(), 0)

    def test_sincronizar_es_idempotente_y_nunca_duplica(self):
        from .offline import encolar_pendiente, sincronizar_pendientes

        movimiento = MovimientoSalida(
            producto=self.producto, fecha=date(2026, 8, 27), tipo="venta", cantidad=2,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("25.00"),
            registrado_por_id=self.user.pk,
        )
        encolar_pendiente(movimiento)
        sincronizar_pendientes()

        # Se vuelve a encolar el MISMO uuid (simula un reintento cuya
        # confirmación anterior se perdió) y se sincroniza 3 veces más.
        encolar_pendiente(movimiento)
        for _ in range(3):
            sincronizar_pendientes()

        self.assertEqual(MovimientoSalida.objects.filter(uuid=movimiento.uuid).count(), 1)

    def test_la_venta_offline_de_un_vendedor_llega_al_stock_al_sincronizar(self):
        from .offline import encolar_pendiente, sincronizar_pendientes

        stock_antes = self.producto.stock_teorico()
        movimiento = MovimientoSalida(
            producto=self.producto, fecha=date(2026, 8, 27), tipo="venta", cantidad=7,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("25.00"),
            registrado_por_id=self.user.pk,
        )
        encolar_pendiente(movimiento)
        sincronizar_pendientes()
        self.assertEqual(self.producto.stock_teorico(), stock_antes - 7)

    def test_las_cuatro_operaciones_se_encolan_y_sincronizan(self):
        from .offline import encolar_pendiente, sincronizar_pendientes

        entrada = LoteCompra(
            producto=self.producto, fecha=date(2026, 8, 27), cantidad=50,
            costo_unitario=Decimal("11.00"), registrado_por_id=self.user.pk,
        )
        venta = MovimientoSalida(
            producto=self.producto, fecha=date(2026, 8, 27), tipo="venta", cantidad=1,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("25.00"),
            registrado_por_id=self.user.pk,
        )
        merma = MovimientoSalida(
            producto=self.producto, fecha=date(2026, 8, 27), tipo="merma", cantidad=2,
            motivo="Botella quebrada", costo_unitario_snapshot=Decimal("10.00"),
            registrado_por_id=self.user.pk,
        )
        conteo = ConteoFisico(
            producto=self.producto, fecha=date(2026, 8, 27), cantidad_contada=140,
            registrado_por_id=self.user.pk,
        )
        for instancia in (entrada, venta, merma, conteo):
            encolar_pendiente(instancia)

        self.assertEqual(sincronizar_pendientes(), 4)
        self.assertTrue(LoteCompra.objects.filter(uuid=entrada.uuid).exists())
        self.assertTrue(MovimientoSalida.objects.filter(uuid=venta.uuid).exists())
        self.assertTrue(MovimientoSalida.objects.filter(uuid=merma.uuid).exists())
        self.assertTrue(ConteoFisico.objects.filter(uuid=conteo.uuid).exists())


class VendedorOfflineTests(TestCase):
    """
    Prompt 19, punto 8 (revalidado en el 19b): la cola no puede
    convertirse en una vía para que un vendedor registre algo que no sea
    una venta.
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        self.user = User.objects.create_user(username="vendedor_limite", password="clave-de-prueba-19b")
        grupo, _ = Group.objects.get_or_create(name="vendedor")
        self.user.groups.add(grupo)
        self.categoria = Categoria.objects.create(nombre="Categoría límite")
        self.producto = Producto.objects.create(
            nombre="Producto límite", categoria=self.categoria, precio_venta_actual=Decimal("30.00")
        )
        self.client.force_login(self.user)

    def test_vendedor_no_puede_registrar_entradas_ni_conteos(self):
        for nombre in ("lotecompra_create", "conteofisico_create"):
            respuesta = self.client.get(reverse(nombre))
            self.assertEqual(respuesta.status_code, 403, nombre)

    def test_vendedor_no_puede_registrar_una_merma_ni_forzando_el_tipo(self):
        respuesta = self.client.post(
            reverse("movimientosalida_create"),
            {"producto": self.producto.pk, "fecha": "2026-08-27", "tipo": "merma",
             "cantidad": 3, "motivo": "forzado a mano", "token_formulario": "x" * 32},
        )
        self.assertEqual(respuesta.status_code, 200)  # re-render con error, no redirección
        self.assertFalse(MovimientoSalida.objects.filter(tipo="merma").exists())


# ---------------------------------------------------------------------------
# Prompt 19c — historial offline, dashboard, refrescar, cola de
# sincronización y validación de stock disponible al vender.
# ---------------------------------------------------------------------------

class StockDisponibleVentaTests(TestCase):
    """
    Prompt 19c, punto 5: no se puede vender más de lo que hay en
    inventario. Cubre producto simple, producto derivado (vía
    factor_equivalencia), que EDITAR una venta ya guardada no dispara la
    validación (evitaría poder corregir nada), y que el cálculo
    considera la cola local de pendientes — no solo el stock ya
    confirmado en Neon.
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        self.user = User.objects.create_user(username="admin_stock", password="clave-de-prueba-19c")
        grupo, _ = Group.objects.get_or_create(name="admin")
        self.user.groups.add(grupo)
        self.categoria = Categoria.objects.create(nombre="Categoría stock")
        self.producto = Producto.objects.create(
            nombre="Corona", categoria=self.categoria, precio_venta_actual=Decimal("18.00")
        )
        LoteCompra.objects.create(
            producto=self.producto, fecha=date(2026, 8, 1), cantidad=10, costo_unitario=Decimal("9.00"),
        )
        self.client.force_login(self.user)

    def _vender(self, producto_id, cantidad, token="tok-" ):
        return self.client.post(
            reverse("movimientosalida_create"),
            {
                "producto": producto_id, "fecha": "2026-08-27", "tipo": "venta",
                "cantidad": cantidad, "motivo": "", "token_formulario": token + str(cantidad) + str(producto_id),
            },
        )

    def test_rechaza_vender_mas_del_stock_disponible(self):
        respuesta = self._vender(self.producto.pk, 11)
        self.assertEqual(respuesta.status_code, 200)  # re-render con error, no redirección
        self.assertContains(respuesta, "No hay suficiente inventario disponible: quedan 10 unidades.")
        self.assertFalse(MovimientoSalida.objects.filter(producto=self.producto).exists())

    def test_permite_vender_exactamente_el_stock_disponible(self):
        respuesta = self._vender(self.producto.pk, 10)
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(
            MovimientoSalida.objects.get(producto=self.producto, tipo="venta").cantidad, 10
        )

    def test_valida_stock_de_un_producto_derivado_via_factor_equivalencia(self):
        base = Producto.objects.create(
            nombre="Gallo base", categoria=self.categoria, precio_venta_actual=Decimal("15.00")
        )
        LoteCompra.objects.create(producto=base, fecha=date(2026, 8, 1), cantidad=12, costo_unitario=Decimal("9.50"))
        cubeta = Producto.objects.create(
            nombre="Cubeta Gallo", categoria=self.categoria, precio_venta_actual=Decimal("85.00"),
            producto_base=base, factor_equivalencia=6,
        )
        # 12 unidades base / 6 por cubeta = 2 cubetas disponibles.
        rechazo = self._vender(cubeta.pk, 3)
        self.assertEqual(rechazo.status_code, 200)
        self.assertContains(rechazo, "No hay suficiente inventario disponible: quedan 2 unidades.")

        exito = self._vender(cubeta.pk, 2)
        self.assertEqual(exito.status_code, 302)
        self.assertEqual(MovimientoSalida.objects.get(producto=cubeta).cantidad, 2)

    def test_editar_una_venta_ya_guardada_no_aplica_la_validacion_de_stock(self):
        """
        stock_teorico() ya cuenta la cantidad ORIGINAL de la venta como
        consumida — corregirla no es "una venta nueva" y no debe exigir
        stock de más por eso. Sin este comportamiento, ni siquiera se
        podría corregir el motivo de una venta que agotó el inventario.
        """
        venta = MovimientoSalida.objects.create(
            producto=self.producto, fecha=date(2026, 8, 20), tipo="venta", cantidad=8,
            costo_unitario_snapshot=Decimal("9.00"), precio_venta_unitario=Decimal("18.00"),
        )
        # Stock real restante: 10 compradas - 8 vendidas = 2.
        self.assertEqual(self.producto.stock_teorico(), 2)

        respuesta = self.client.post(
            reverse("movimientosalida_correccion_editar", args=[venta.pk]),
            {
                "producto": self.producto.pk, "fecha": "2026-08-20", "tipo": "venta",
                "cantidad": 9, "motivo": "corrección de prueba — cantidad real era 9, no 8",
            },
        )
        self.assertEqual(respuesta.status_code, 302, respuesta.content[:500])
        venta.refresh_from_db()
        self.assertEqual(venta.cantidad, 9)

    def test_stock_disponible_para_venta_descuenta_la_cola_local_de_pendientes(self):
        """
        Dos ventas offline seguidas del mismo producto en la misma
        máquina: la segunda no debe "ver" el mismo stock cacheado que la
        primera — tiene que descontar lo que la primera ya reservó en la
        cola local, aunque ninguna de las dos haya llegado todavía a Neon.
        """
        from unittest.mock import patch

        from .models import PendienteSincronizacion
        from .offline import encolar_pendiente, refrescar_catalogo_cache, stock_disponible_para_venta

        # refrescar_catalogo_cache() (con conexión, como pasaría de
        # verdad) deja el producto en "local_disco" con su stock real (10)
        # en stock_teorico_cache — no se arma la fila a mano para probar
        # el camino real que usa la app.
        refrescar_catalogo_cache()

        with patch("inventario.offline.hay_conexion", return_value=False):
            self.assertEqual(stock_disponible_para_venta(
                Producto.objects.using("local_disco").get(pk=self.producto.pk)
            ), 10)

            pendiente = MovimientoSalida(
                producto_id=self.producto.pk, fecha=date(2026, 8, 27), tipo="venta", cantidad=7,
                costo_unitario_snapshot=Decimal("9.00"), precio_venta_unitario=Decimal("18.00"),
            )
            encolar_pendiente(pendiente)

            disponible = stock_disponible_para_venta(
                Producto.objects.using("local_disco").get(pk=self.producto.pk)
            )
            self.assertEqual(disponible, 3, "No descontó la venta ya encolada pero aún sin sincronizar.")

        PendienteSincronizacion.objects.using("local_disco").filter(uuid=pendiente.uuid).delete()


class HistorialOfflineTests(TestCase):
    """
    Prompt 19c, punto 1: Historial en modo lectura combina la caché local
    de movimientos ya sincronizados con la cola de pendientes de esta
    máquina, marcando estos últimos como "es_pendiente".
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        from .models import MovimientoHistorialCache, PendienteSincronizacion

        MovimientoHistorialCache.objects.using("local_disco").all().delete()
        PendienteSincronizacion.objects.using("local_disco").all().delete()

        self.user = User.objects.create_user(username="auditor_hist_off", password="clave-de-prueba-19c")
        grupo, _ = Group.objects.get_or_create(name="auditor")
        self.user.groups.add(grupo)
        self.categoria = Categoria.objects.create(nombre="Categoría historial offline")
        self.producto = Producto.objects.create(
            nombre="Producto historial offline", categoria=self.categoria, precio_venta_actual=Decimal("20.00")
        )
        # refrescar_catalogo_cache() (no una copia manual) para que
        # Categoria/Producto queden en "local_disco" con el mismo pk que
        # en "default" — es justo lo que hace el hilo de fondo con
        # conexión, y evita duplicar a mano cómo se arma esa caché.
        from .offline import refrescar_catalogo_cache

        refrescar_catalogo_cache()

    def test_historial_offline_combina_cache_y_pendientes(self):
        from .models import MovimientoHistorialCache
        from .offline import encolar_pendiente, historial_offline

        MovimientoHistorialCache.objects.using("local_disco").create(
            tipo_registro="MovimientoSalida", registro_id=999, fecha=date(2026, 8, 20), producto_id=self.producto.pk,
            payload={
                "fecha": {"__date__": "2026-08-20"}, "tipo": "Venta", "producto_nombre": self.producto.nombre,
                "cantidad": 4, "valor_unitario": {"__decimal__": "20.00"}, "usuario": "auditor_hist_off",
                "detalle": "", "creado_en": {"__datetime__": "2026-08-20T10:00:00+00:00"},
                "tipo_registro": "MovimientoSalida", "registro_id": 999,
            },
        )
        pendiente = MovimientoSalida(
            producto_id=self.producto.pk, fecha=date(2026, 8, 27), tipo="venta", cantidad=2,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("20.00"),
            registrado_por_id=self.user.pk,
        )
        encolar_pendiente(pendiente)

        filas = historial_offline(date(2026, 1, 1), date(2026, 12, 31))

        self.assertEqual(len(filas), 2)
        pendientes = [f for f in filas if f["es_pendiente"]]
        sincronizadas = [f for f in filas if not f["es_pendiente"]]
        self.assertEqual(len(pendientes), 1)
        self.assertEqual(len(sincronizadas), 1)
        self.assertEqual(pendientes[0]["cantidad"], 2)
        self.assertEqual(pendientes[0]["registro_id"], None)
        self.assertEqual(sincronizadas[0]["cantidad"], 4)
        # Más reciente primero: la pendiente (27) antes que la cacheada (20).
        self.assertTrue(filas[0]["es_pendiente"])

    def test_vista_historial_offline_no_crashea_y_marca_pendientes(self):
        from unittest.mock import patch

        from .offline import encolar_pendiente

        pendiente = MovimientoSalida(
            producto_id=self.producto.pk, fecha=date(2026, 8, 27), tipo="venta", cantidad=3,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("20.00"),
            registrado_por_id=self.user.pk,
        )
        encolar_pendiente(pendiente)
        self.client.force_login(self.user)

        with patch("inventario.views.hay_conexion", return_value=False):
            respuesta = self.client.get(reverse("historial"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Pendiente")
        self.assertContains(respuesta, "Sin conexión")

    def test_editar_historial_sigue_bloqueado_sin_conexion_aunque_historial_ya_no_lo_este(self):
        from unittest.mock import patch

        venta = MovimientoSalida.objects.create(
            producto=self.producto, fecha=date(2026, 8, 1), tipo="venta", cantidad=1,
            costo_unitario_snapshot=Decimal("10.00"), precio_venta_unitario=Decimal("20.00"),
        )
        self.user.groups.clear()
        grupo_admin, _ = Group.objects.get_or_create(name="admin")
        self.user.groups.add(grupo_admin)
        self.client.force_login(self.user)

        # RequiereConexionMixin (el que de verdad bloquea editar/eliminar
        # historial) vive en inventario/resiliencia.py, con su PROPIO
        # "from .offline import hay_conexion" — un nombre aparte del de
        # inventario.views, aunque apunten a la misma función original.
        # Parchear solo uno de los dos no alcanza para simular "sin
        # conexión" en esta vista.
        with patch("inventario.views.hay_conexion", return_value=False), \
             patch("inventario.resiliencia.hay_conexion", return_value=False):
            respuesta = self.client.get(reverse("movimientosalida_correccion_editar", args=[venta.pk]))

        self.assertEqual(respuesta.status_code, 503)
        self.assertContains(respuesta, "Esta función requiere conexión a internet", status_code=503)


class ColaSincronizacionTests(TestCase):
    """Prompt 19c, punto 4: pantalla de cola de sincronización para admin/auditor."""

    databases = {"default", "local_disco"}

    def setUp(self):
        from .models import PendienteSincronizacion

        PendienteSincronizacion.objects.using("local_disco").all().delete()
        self.admin = User.objects.create_user(username="admin_cola", password="clave-de-prueba-19c")
        grupo_admin, _ = Group.objects.get_or_create(name="admin")
        self.admin.groups.add(grupo_admin)
        self.vendedor = User.objects.create_user(username="vendedor_cola_19c", password="clave-de-prueba-19c")
        grupo_vendedor, _ = Group.objects.get_or_create(name="vendedor")
        self.vendedor.groups.add(grupo_vendedor)

        self.categoria = Categoria.objects.create(nombre="Categoría cola 19c")
        self.producto = Producto.objects.create(
            nombre="Producto cola 19c", categoria=self.categoria, precio_venta_actual=Decimal("12.00")
        )
        from .offline import refrescar_catalogo_cache

        refrescar_catalogo_cache()

    def test_vendedor_no_tiene_acceso_a_la_pantalla_completa(self):
        self.client.force_login(self.vendedor)
        respuesta = self.client.get(reverse("cola_sincronizacion"))
        self.assertEqual(respuesta.status_code, 403)

    def test_lista_los_pendientes_reales_de_la_cola(self):
        from .offline import encolar_pendiente

        lote = LoteCompra(
            producto_id=self.producto.pk, fecha=date(2026, 8, 27), cantidad=50, costo_unitario=Decimal("6.00"),
        )
        encolar_pendiente(lote)
        self.client.force_login(self.admin)

        respuesta = self.client.get(reverse("cola_sincronizacion"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Producto cola 19c")
        self.assertContains(respuesta, "Entrada")

        from .models import PendienteSincronizacion

        PendienteSincronizacion.objects.using("local_disco").filter(uuid=lote.uuid).delete()

    def test_reintentar_todos_sincroniza_lo_pendiente(self):
        from .models import PendienteSincronizacion
        from .offline import encolar_pendiente

        lote = LoteCompra(
            producto_id=self.producto.pk, fecha=date(2026, 8, 27), cantidad=15, costo_unitario=Decimal("6.50"),
        )
        encolar_pendiente(lote)
        self.client.force_login(self.admin)

        respuesta = self.client.post(reverse("cola_sincronizacion_reintentar_todos"), follow=True)

        self.assertEqual(respuesta.status_code, 200)
        self.assertFalse(PendienteSincronizacion.objects.using("local_disco").filter(uuid=lote.uuid).exists())
        self.assertTrue(LoteCompra.objects.filter(uuid=lote.uuid).exists())

    def test_reintentar_uno_reporta_un_fallo_no_recuperable_sin_perder_el_pendiente(self):
        """
        Fuerza un fallo real de sincronización, no de conexión: un
        payload con un campo que ya no existe en el modelo (simula un
        pendiente encolado antes de un cambio de esquema) — Modelo(...)
        revienta con TypeError al crear la instancia, antes de tocar la
        base. Confirma que el botón "reintentar ahora" no lo pierde ni lo
        sincroniza a la fuerza, y que queda registrado el error.
        """
        import uuid as uuid_module

        from django.utils import timezone

        from .models import PendienteSincronizacion

        pendiente = PendienteSincronizacion.objects.using("local_disco").create(
            uuid=uuid_module.uuid4(),
            modelo="LoteCompra",
            payload={
                "producto_id": self.producto.pk, "fecha": {"__date__": "2026-08-27"}, "cantidad": 5,
                "costo_unitario": {"__decimal__": "1.00"}, "proveedor": None, "notas": None,
                "registrado_por_id": None, "campo_que_ya_no_existe": "valor",
            },
            creado_en=timezone.now(),
        )
        self.client.force_login(self.admin)

        respuesta = self.client.post(
            reverse("cola_sincronizacion_reintentar", args=[pendiente.pk]), follow=True
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "No se pudo sincronizar")
        pendiente.refresh_from_db(using="local_disco")
        self.assertEqual(pendiente.intentos, 1)
        self.assertTrue(pendiente.ultimo_error)
        self.assertFalse(LoteCompra.objects.filter(uuid=pendiente.uuid).exists())

        pendiente.delete(using="local_disco")


class SondeoConcurrenteTests(SimpleTestCase):
    """
    Un corte de red no puede costar una espera POR CADA hilo (prompt 33c).

    Medido en la VM de Windows con el adaptador apagado: un solo sondeo
    tardó 28.50s —el DNS se cuelga y connect_timeout no cubre la
    resolución, solo se aplica después de resolver— y tres inicios de
    sesión simultáneos pagaron cada uno su propio atasco:

        10165ms  POST 302  /login/
        38670ms  POST 302  /login/
        29373ms  POST 302  /login/

    La caché de resultado negativo no los salvaba: no se marca hasta que
    el PRIMER sondeo termina, así que los tres entraron antes de que
    hubiera nada que leer. Con 4 hilos en waitress, eso es la app entera
    detenida.
    """

    # Declarado a propósito: un sondeo SANO llega hasta
    # ensure_connection(), y SimpleTestCase bloquea toda consulta salvo
    # que se declaren los alias. Sin esto, el sondeo "sano" de la prueba
    # de la regresión del 33d fallaría por el bloqueo de Django y
    # estaríamos midiendo otra cosa.
    databases = {"default"}

    def setUp(self):
        from .offline import reiniciar_cache_conexion

        reiniciar_cache_conexion()
        self.addCleanup(reiniciar_cache_conexion)

    def _sondeo_lento(self, duracion, falla):
        """
        Sustituye _reciclar_conexion_remota por uno que tarda `duracion`
        y termina en error o en éxito según `falla`.

        Se parchea esa función del MÓDULO y no
        connections["default"].ensure_connection: el registro de
        conexiones de Django es por hilo, así que parchear el objeto de
        conexión desde el hilo principal no le llega al hilo que sondea,
        y la prueba mediría otra cosa. (Costó un falso "pasa" descubrirlo.)
        """
        import time

        from django.db.utils import OperationalError

        from . import offline

        original = offline._reciclar_conexion_remota

        def reciclar_lento():
            time.sleep(duracion)
            if falla:
                raise OperationalError("connection timeout expired (simulado)")

        offline._reciclar_conexion_remota = reciclar_lento
        self.addCleanup(setattr, offline, "_reciclar_conexion_remota", original)

    def test_un_sondeo_sano_pero_lento_no_hace_que_los_demas_se_crean_offline(self):
        """
        LA REGRESIÓN DEL PROMPT 33D, blindada.

        Hasta el 33d, si un sondeo llevaba más de 2s los demás hilos
        DABAN POR HECHO que no había conexión. La justificación escrita
        era "un sondeo sano contra Neon tarda menos de 1s", medida en
        Mac; al diagnosticar el 33d, el primer sondeo sano en esa misma
        Mac tardó 1.936s — 64 ms por debajo del umbral. En la VM de
        Windows lo superaba de sobra, y como get_new_connection() consulta
        esa conjetura para decidir si siquiera intenta conectar, bastaba
        para que un login con internet perfecto se resolviera contra la
        caché local.

        Ahora los demás ESPERAN el resultado real. Esta prueba fija que un
        sondeo sano pero lento devuelva True a todos, no False.
        """
        import threading
        import time

        from . import offline

        DURACION_SONDEO = 4.0
        self._sondeo_lento(DURACION_SONDEO, falla=False)

        resultado_sondeador = {}

        def sondear():
            resultado_sondeador["valor"] = offline.hay_conexion()

        hilo = threading.Thread(target=sondear)
        hilo.start()
        time.sleep(2.5)  # más de los 2s que antes bastaban para asumir caída

        self.assertFalse(
            offline.sin_conexion_reciente(),
            "Un sondeo EN CURSO no es evidencia de que no haya conexión. "
            "sin_conexion_reciente() solo debe reportar un sondeo que YA FALLÓ.",
        )
        self.assertTrue(
            offline.hay_conexion(),
            "El segundo hilo se creyó sin conexión mientras un sondeo SANO seguía en "
            "curso — esa es exactamente la regresión del prompt 33d.",
        )

        hilo.join(timeout=DURACION_SONDEO + 5)
        self.assertFalse(hilo.is_alive(), "El hilo que sondea no terminó.")
        self.assertTrue(resultado_sondeador["valor"])

    def test_mientras_un_hilo_averigua_los_demas_no_lanzan_su_propio_sondeo(self):
        """
        La otra mitad, que NO se puede perder al arreglar la de arriba:
        sigue sin haber avalancha. Los hilos que llegan durante un sondeo
        esperan SU resultado en vez de abrir cada uno el suyo — que es lo
        que producía tres logins de 10, 29 y 38 segundos a la vez (33c).
        """
        import threading
        import time

        from . import offline

        DURACION_SONDEO = 3.0
        self._sondeo_lento(DURACION_SONDEO, falla=True)

        sondeos_lanzados = {"n": 0}
        original = offline._reciclar_conexion_remota

        def contar_y_sondear():
            sondeos_lanzados["n"] += 1
            return original()

        offline._reciclar_conexion_remota = contar_y_sondear
        self.addCleanup(setattr, offline, "_reciclar_conexion_remota", original)

        resultados = []

        def pedir():
            resultados.append(offline.hay_conexion())

        hilos = [threading.Thread(target=pedir) for _ in range(4)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=DURACION_SONDEO + 8)

        self.assertEqual(len(resultados), 4, "Algún hilo se quedó colgado.")
        self.assertTrue(all(r is False for r in resultados), "Todos deben ver el mismo resultado real.")
        self.assertEqual(
            sondeos_lanzados["n"], 1,
            f"Se lanzaron {sondeos_lanzados['n']} sondeos en paralelo en vez de 1: volvió "
            "la avalancha del 33c.",
        )

    def test_la_cache_dura_al_menos_el_doble_de_lo_que_costo_averiguarlo(self):
        """
        Con un margen fijo de 15s y un sondeo de 28.5s, la caché nacía
        vencida y el siguiente hilo volvía a pagar la espera entera.
        """
        import time

        from django.db.utils import OperationalError

        from . import offline

        DURACION_SONDEO = 3.0
        original = offline._reciclar_conexion_remota

        def reciclar_lento():
            time.sleep(DURACION_SONDEO)
            raise OperationalError("connection timeout expired (simulado)")

        offline._reciclar_conexion_remota = reciclar_lento
        self.addCleanup(setattr, offline, "_reciclar_conexion_remota", original)

        self.assertFalse(offline.hay_conexion())
        restante = offline._sin_conexion_hasta - time.monotonic()
        self.assertGreaterEqual(
            restante, DURACION_SONDEO * 2 - 0.5,
            f"La caché quedó válida solo {restante:.1f}s tras un sondeo de "
            f"{DURACION_SONDEO}s: averiguarlo sale más caro que no averiguarlo.",
        )


class ConfiguracionNubeAusenteTests(SimpleTestCase):
    """
    Prompt 33 — el bug que dejó el motor offline completamente inerte en
    el .exe de Windows.

    Confirmado con el log de la VM: al faltar el .env junto al
    ejecutable, settings.py caía en silencio a un SQLite local. Con
    SQLite como base "default", hay_conexion() devuelve True SIEMPRE (un
    archivo local siempre "conecta", medido en 1ms), así que el motor
    offline nunca se enteraba de que no había nube y jamás se activaba:
    el login se validaba contra una base vacía y cada movimiento se
    encolaba y se DESENCOLABA de inmediato, porque la escritura al SQLite
    local sí tenía éxito. Todo terminaba en un db.sqlite3 huérfano que no
    sincronizaba con nada.
    """

    databases = {"default"}

    def setUp(self):
        from .offline import reiniciar_cache_conexion

        reiniciar_cache_conexion()
        self.addCleanup(reiniciar_cache_conexion)

    def test_sin_configuracion_de_nube_hay_conexion_es_false_al_instante(self):
        """
        Lo que hace que el motor offline SÍ se active: sin configuración
        legible nunca hay conexión, y se responde sin sondear la red ni
        una sola vez (si sondeara, pagaría el timeout en cada request).
        """
        import time

        from django.test import override_settings

        from .offline import hay_conexion

        with override_settings(BD_NUBE_NO_CONFIGURADA=True):
            inicio = time.monotonic()
            resultado = hay_conexion()
            duracion = time.monotonic() - inicio

        self.assertFalse(resultado, "Con la nube sin configurar, hay_conexion() debe decir False.")
        self.assertLess(duracion, 0.5, "No debe sondear la red: tiene que responder al instante.")

    def test_el_alias_default_nunca_es_sqlite_en_un_build_empaquetado(self):
        """
        La regla que evita el bug de raíz: un .exe JAMÁS debe caer a
        SQLite para el alias "default". Da igual que sea más "amable" —
        es justo lo que hacía que hay_conexion() mintiera y que todo se
        guardara en una base paralela que nunca sincroniza. En un build
        empaquetado sin configuración, "default" queda apuntando a un
        Postgres inalcanzable a propósito, para que toda consulta falle
        como un corte de conexión normal y el motor offline funcione.
        """
        import re

        from pathlib import Path

        fuente = (Path(__file__).resolve().parent.parent / "auditoria_aylupita" / "settings.py").read_text(encoding="utf-8")
        # La condición de la rama puede crecer (desde el prompt 33c
        # también entra aquí una DATABASE_URL presente pero inservible),
        # así que se busca "elif esta_empaquetado()" seguido de lo que
        # sea hasta los dos puntos — no la línea literal, que ya se rompió
        # una vez por ampliarla.
        rama_empaquetado = re.search(
            r"elif esta_empaquetado\(\)[^:\n]*:(.*?)\nelse:", fuente, re.S
        )
        self.assertIsNotNone(
            rama_empaquetado,
            "settings.py ya no tiene la rama 'elif esta_empaquetado()' que impide el fallback a SQLite.",
        )
        # Solo el CÓDIGO, sin comentarios: la explicación de por qué no se
        # usa SQLite menciona "sqlite3" varias veces a propósito.
        cuerpo = "\n".join(
            linea for linea in rama_empaquetado.group(1).splitlines()
            if not linea.strip().startswith("#")
        )
        self.assertNotIn(
            "sqlite3", cuerpo,
            "Un build empaquetado sin configuración volvió a caer a SQLite — ese es exactamente "
            "el bug del prompt 33: hay_conexion() diría True siempre y el motor offline quedaría inerte.",
        )
        self.assertIn("BD_NUBE_NO_CONFIGURADA = True", cuerpo)

    def test_una_database_url_invalida_no_revienta_el_arranque_ni_cae_a_sqlite(self):
        """
        Prompt 33c, punto 2. Dos fallos distintos que compartían una
        misma causa —"hay DATABASE_URL" no es lo mismo que "DATABASE_URL
        sirve"— y que hay que verificar importando settings DE VERDAD,
        en un proceso aparte, porque el daño ocurría al importarlo:

        - 'basura' hacía que dj_database_url lanzara UnknownSchemeError
          al importar settings.py, o sea que la app no arrancaba en
          absoluto: ni ventana, ni log, ni mensaje. Imposible de
          diagnosticar desde la VM.
        - 'postgresql://' a secas sí importaba, pero dejaba HOST y NAME
          vacíos sin marcar nada como "sin configurar", así que la
          interfaz mostraba el aviso de corte de red normal en lugar del
          de problema de configuración.

        En ninguno de los dos casos "default" puede terminar en SQLite:
        eso es lo que hacía que hay_conexion() dijera True siempre y el
        motor offline quedara inerte.
        """
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        raiz = Path(__file__).resolve().parent.parent
        guion = (
            "import django, json, os; "
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'auditoria_aylupita.settings'); "
            "django.setup(); "
            "from django.conf import settings as s; "
            "cfg = s.DATABASES['default']; "
            "print(json.dumps({'engine': cfg['ENGINE'], 'host': cfg.get('HOST'), "
            "'no_configurada': s.BD_NUBE_NO_CONFIGURADA, "
            "'motivo': s.BD_MOTIVO_NO_CONFIGURADA}))"
        )

        casos = {
            "basura": "no se pudo interpretar",
            "postgresql://": "host",
            "   ": "no está definida",
            "sqlite:////tmp/x.db": "PostgreSQL",
        }
        for url, fragmento_esperado in casos.items():
            with self.subTest(database_url=url):
                entorno = dict(os.environ, DATABASE_URL=url, DIRECT_DATABASE_URL="")
                proceso = subprocess.run(
                    [sys.executable, "-c", guion],
                    cwd=str(raiz), env=entorno, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(
                    proceso.returncode, 0,
                    f"Importar settings con DATABASE_URL={url!r} falló — la app no arrancaría "
                    f"ni para mostrar el aviso:\n{proceso.stderr[-2000:]}",
                )
                datos = json.loads(proceso.stdout.strip().splitlines()[-1])
                self.assertNotIn(
                    "sqlite", datos["engine"],
                    f"Con DATABASE_URL={url!r} el alias 'default' cayó a SQLite. Eso es el bug "
                    "del prompt 33: una base local siempre 'conecta', así que el motor offline "
                    "nunca se entera de que no hay nube.",
                )
                self.assertTrue(
                    datos["no_configurada"],
                    f"Con DATABASE_URL={url!r} la app no quedó marcada como 'sin configuración', "
                    "así que mostraría el aviso de corte de red normal en vez del de "
                    "configuración — y nadie llamaría a soporte.",
                )
                self.assertIn(
                    fragmento_esperado, datos["motivo"] or "",
                    "El motivo tiene que decir QUÉ está mal: es lo que se muestra en la "
                    "interfaz como 'Detalle para soporte' y lo que evita pedir el log.",
                )

    def test_el_motivo_llega_a_la_interfaz_y_no_solo_al_log(self):
        """
        Prompt 33c, punto 2: el aviso debe verse en la INTERFAZ, no solo
        en diagnostico.log — a Ruth y Michelle nadie les va a pedir que
        abran un archivo de texto. Y tiene que ser DISTINTO del aviso de
        "sin conexión", porque uno se arregla solo y el otro no.
        """
        from django.test import override_settings

        from .context_processors import configuracion_bd

        with override_settings(
            BD_NUBE_NO_CONFIGURADA=True,
            BD_MOTIVO_NO_CONFIGURADA="DATABASE_URL no está definida",
        ):
            contexto = configuracion_bd(None)

        self.assertTrue(contexto["bd_nube_no_configurada"])
        self.assertEqual(contexto["bd_motivo_no_configurada"], "DATABASE_URL no está definida")

        from pathlib import Path

        base = Path(__file__).resolve().parent.parent / "templates" / "base.html"
        plantilla = base.read_text(encoding="utf-8")
        self.assertIn("bd_nube_no_configurada", plantilla)
        self.assertIn("bd_motivo_no_configurada", plantilla)
        self.assertIn("no se arregla reconectándote a internet", plantilla)

    def test_la_conexion_a_la_nube_lleva_timeouts_explicitos(self):
        """
        Sin connect_timeout, un corte de red real (paquetes perdidos, no
        un puerto que rechaza) hace esperar el timeout del sistema
        operativo — 75s medidos — en CADA sondeo, y con waitress en 4
        hilos eso deja la app entera sin responder. Los keepalives cubren
        el otro caso: una conexión ya establecida que muere.
        """
        from django.conf import settings

        cfg = settings.DATABASES["default"]
        if not _es_postgres(cfg["ENGINE"]):
            self.skipTest("Solo aplica corriendo contra Postgres/Neon (hay .env configurado).")

        opciones = cfg.get("OPTIONS") or {}
        self.assertIn("connect_timeout", opciones, "Falta connect_timeout: ver el prompt 33.")
        self.assertLessEqual(opciones["connect_timeout"], 15)
        self.assertEqual(opciones.get("keepalives"), 1, "Faltan keepalives para detectar una conexión muerta.")


class DiscrepanciasInventarioTests(TestCase):
    """
    Los cuatro escenarios del prompt 34, con los números exactos.

    El fallo de fondo que blindan: el sistema comparaba un conteo físico
    contra el stock teórico "de su fecha" —y `fecha` es un DateField, sin
    hora— así que un conteo de las 15:03 se medía contra un teórico que ya
    incluía una venta de las 15:05. La venta se contaba dos veces: una en
    el teórico y otra en la realidad del piso. Encima, la discrepancia no
    era un registro sino una resta en vivo, así que un movimiento
    posterior podía hacerla desaparecer de la pantalla sin que nadie la
    revisara.
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        from django.utils import timezone as tz

        self.hoy = date(2026, 8, 31)
        categoria = Categoria.objects.create(nombre="Cervezas 34")
        self.gallo = Producto.objects.create(
            nombre="Gallo 34", categoria=categoria, precio_venta_actual=Decimal("15.00")
        )
        self.ruth = User.objects.create_user(username="ruth34", password="x")
        self.michelle = User.objects.create_user(username="michelle34", password="x")
        self.ventas = User.objects.create_user(username="ventas34", password="x")
        self.tz = tz

    def _instante(self, hora, minuto, segundo=0):
        """Un instante del día de la prueba, en la zona del servidor."""
        from django.utils import timezone as tz

        ingenuo = datetime(self.hoy.year, self.hoy.month, self.hoy.day, hora, minuto, segundo)
        return tz.make_aware(ingenuo, tz.get_current_timezone())

    def _stock_inicial(self, cantidad, hora=8):
        return LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad=cantidad,
            costo_unitario=Decimal("6.50"), registrado_por=self.ruth,
            ocurrido_en=self._instante(hora, 0),
        )

    def _venta(self, cantidad, hora, minuto, segundo=0, usuario=None):
        return MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=cantidad,
            precio_venta_unitario=Decimal("15.00"),
            registrado_por=usuario or self.ruth,
            ocurrido_en=self._instante(hora, minuto, segundo),
        )

    def _conteo(self, cantidad, hora, minuto, segundo=0, usuario=None):
        return ConteoFisico.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=cantidad,
            registrado_por=usuario or self.michelle,
            ocurrido_en=self._instante(hora, minuto, segundo),
        )

    # --- Escenario 1 ---------------------------------------------------

    def test_escenario_1_el_ajuste_se_calcula_contra_el_momento_del_conteo(self):
        """
        430 inicial. Ruth vende 6 a las 15:05:30 CON conexión. Michelle
        cuenta 425 a las 15:03:20 SIN conexión y sincroniza después.

        Antes: 425 contra 424 (teórico del día, que ya descuenta la venta
        de Ruth) = "+1 sobrante".
        Correcto: 425 contra 430 (teórico a las 15:03:20) = "5 faltantes".
        """
        self._stock_inicial(430)
        self.assertEqual(self.gallo.stock_teorico(), 430)

        self._venta(6, 15, 5, 30, usuario=self.ruth)
        conteo = self._conteo(425, 15, 3, 20, usuario=self.michelle)

        # El número viejo, para que quede constancia de qué se corrigió.
        self.assertEqual(
            self.gallo.stock_teorico(hasta_fecha=self.hoy), 424,
            "El teórico al cierre del día sí incluye la venta de Ruth — no es el ancla correcta.",
        )

        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        self.assertEqual(discrepancia.teorico_al_conteo, 430)
        self.assertEqual(discrepancia.diferencia, -5, "Faltan 5, no sobra 1.")
        self.assertEqual(discrepancia.estado, DiscrepanciaInventario.PENDIENTE)
        self.assertIsNone(discrepancia.ajuste, "No se puede haber movido stock solo.")

    # --- Escenario 2 ---------------------------------------------------

    def test_escenario_2_ningun_ajuste_se_aplica_sin_una_persona(self):
        """
        Del escenario 1 no puede salir ningún movimiento de stock por su
        cuenta: el ajuste parte de un número que un humano tiene que
        confirmar primero.
        """
        self._stock_inicial(430)
        self._venta(6, 15, 5, 30)
        conteo = self._conteo(425, 15, 3, 20)

        self.assertEqual(
            MovimientoSalida.objects.filter(tipo="ajuste").count(), 0,
            "El sistema generó un ajuste sin que nadie lo confirmara.",
        )
        self.assertIsNone(conteo.ajuste_generado_id)
        self.assertEqual(self.gallo.stock_teorico(), 424, "El stock no debe haberse tocado.")

        # Y cuando SÍ lo confirma una persona, se aplica lo que ella diga.
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        resolver_discrepancia(discrepancia, discrepancia.ajuste_sugerido, self.ruth, "Revisado")
        discrepancia.refresh_from_db()

        self.assertEqual(discrepancia.estado, DiscrepanciaInventario.RESUELTA)
        self.assertEqual(discrepancia.cantidad_confirmada, 5, "Faltan 5: el ajuste RESTA 5.")
        self.assertEqual(discrepancia.resuelta_por, self.ruth)
        self.assertEqual(self.gallo.stock_teorico(), 419, "424 − 5 = 419.")

    # --- Escenario 3 ---------------------------------------------------

    def test_escenario_3_una_venta_posterior_no_resuelve_la_discrepancia(self):
        """
        Con la discrepancia abierta, Ventas registra 1 botella en otro
        equipo. Antes la resta daba cero y la alerta desaparecía del
        tablero sin que nadie la hubiera revisado.
        """
        self._stock_inicial(430)
        self._venta(6, 15, 5, 30)
        conteo = self._conteo(425, 15, 3, 20)
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        self.assertEqual(discrepancia.diferencia, -5)

        self._venta(1, 16, 0, 0, usuario=self.ventas)

        discrepancia.refresh_from_db()
        self.assertEqual(
            discrepancia.estado, DiscrepanciaInventario.PENDIENTE,
            "La discrepancia se cerró sola por una venta posterior.",
        )
        self.assertEqual(
            discrepancia.diferencia, -5,
            "La diferencia congelada cambió: dejó de ser un registro y volvió a ser una resta.",
        )
        self.assertFalse(
            discrepancia.requiere_revision,
            "Una venta POSTERIOR al conteo no cambia su pasado: no hay nada que revisar.",
        )
        self.assertEqual(
            DiscrepanciaInventario.objects.filter(estado=DiscrepanciaInventario.PENDIENTE).count(), 1,
            "La discrepancia tiene que seguir visible como pendiente.",
        )

    # --- Escenario adicional: alertas acumuladas -----------------------

    def test_escenario_acumulado_dos_discrepancias_no_se_pisan(self):
        """
        Teórico 427. Un conteo registra 430 (+3, sin resolver). Otro
        registra 420, y debe dar −7 contra los 427 que le corresponden —
        no −10, que es lo que salía cuando el ajuste de la primera alerta
        ya había subido el teórico a 430.
        """
        self._stock_inicial(427)

        conteo_a = self._conteo(430, 10, 0, 0)
        discrepancia_a = DiscrepanciaInventario.objects.get(conteo=conteo_a)
        self.assertEqual(discrepancia_a.diferencia, 3, "430 − 427 = +3 sobrantes.")

        conteo_b = self._conteo(420, 15, 0, 0)
        discrepancia_b = DiscrepanciaInventario.objects.get(conteo=conteo_b)
        self.assertEqual(
            discrepancia_b.diferencia, -7,
            "La primera alerta sigue pendiente y no movió nada: el segundo conteo se compara "
            "contra 427, no contra 430.",
        )
        self.assertEqual(discrepancia_a.diferencia, 3, "La primera no puede haber cambiado.")

    def test_resolver_la_mas_antigua_marca_la_posterior_para_revisar(self):
        """
        Decisión 1 del diseño: resolver A (10:00) después de que B (15:00)
        quedó congelada cambia el pasado de B. No se corrige solo — se
        recalcula aparte y se marca, con el número viejo y el nuevo a la
        vista.
        """
        self._stock_inicial(427)
        conteo_a = self._conteo(430, 10, 0, 0)
        conteo_b = self._conteo(420, 15, 0, 0)
        discrepancia_a = DiscrepanciaInventario.objects.get(conteo=conteo_a)
        discrepancia_b = DiscrepanciaInventario.objects.get(conteo=conteo_b)

        resolver_discrepancia(discrepancia_a, discrepancia_a.ajuste_sugerido, self.ruth, "Sobrante confirmado")

        discrepancia_b.refresh_from_db()
        self.assertTrue(discrepancia_b.requiere_revision)
        self.assertEqual(discrepancia_b.diferencia, -7, "El número original se conserva.")
        self.assertEqual(discrepancia_b.diferencia_recalculada, -10, "Y el nuevo se guarda aparte.")
        self.assertEqual(
            discrepancia_b.estado, DiscrepanciaInventario.PENDIENTE,
            "Marcar para revisar no es resolver.",
        )
        self.assertIn("#%d" % discrepancia_a.pk, discrepancia_b.motivo_revision)

    def test_un_movimiento_con_fecha_hacia_atras_marca_las_discrepancias(self):
        """
        Decisión 2: mismo problema por otra puerta — alguien registra hoy
        una compra con fecha anterior al conteo.
        """
        self._stock_inicial(427)
        conteo = self._conteo(420, 15, 0, 0)
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        self.assertEqual(discrepancia.diferencia, -7)

        LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy - timedelta(days=1), cantidad=10,
            costo_unitario=Decimal("6.50"), registrado_por=self.ruth,
            ocurrido_en=self._instante(17, 0),
        )

        discrepancia.refresh_from_db()
        self.assertTrue(discrepancia.requiere_revision)
        self.assertEqual(discrepancia.diferencia_recalculada, -17, "420 − 437 = −17.")

    def test_un_reloj_adelantado_queda_marcado(self):
        """
        Decisión 4: no se corrige el reloj, pero un desfase imposible —un
        registro creado DESPUÉS de haber sido recibido— queda marcado en
        vez de reordenar el inventario en silencio.
        """
        from django.utils import timezone as tz

        futuro = MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=1,
            precio_venta_unitario=Decimal("15.00"), registrado_por=self.ventas,
            ocurrido_en=tz.now() + timedelta(hours=3),
        )
        normal = self._venta(1, 12, 0)

        self.assertTrue(futuro.reloj_sospechoso, "Un instante en el futuro tiene que marcarse.")
        self.assertFalse(normal.reloj_sospechoso)

    def test_el_detalle_del_reporte_muestra_la_diferencia_congelada(self):
        """
        Lo encontró el Excel de este mismo prompt: el detalle del conteo
        recalculaba la resta contra el stock ACTUAL, así que después de
        aplicarle su propio ajuste un conteo cuya diferencia real era −5
        aparecía como "+7" — un número sin ningún significado, dentro de
        un reporte financiero.
        """
        from .services import movimientos_periodo

        self._stock_inicial(430)
        self._venta(6, 15, 5, 30)
        conteo = self._conteo(425, 15, 3, 20)
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        resolver_discrepancia(discrepancia, discrepancia.ajuste_sugerido, self.ruth, "Faltante real")

        filas = movimientos_periodo(self.hoy, self.hoy)
        detalle_conteo = next(f["detalle"] for f in filas if f["tipo"] == "Conteo físico")

        self.assertIn("Diferencia: -5", detalle_conteo)
        self.assertNotIn("+7", detalle_conteo)
        self.assertIn("ajuste aplicado (+5)", detalle_conteo)

    def test_un_faltante_confirmado_aparece_en_el_reporte(self):
        """
        Un ajuste confirmado mueve producto real: tiene que verse en el
        reporte. El resumen ya calculaba `unidades_ajuste` pero no lo
        mostraba ni en pantalla ni en el Excel, así que un faltante
        confirmado no aparecía por ninguna parte.
        """
        from .reportes import COLUMNAS_RESUMEN
        from .services import resumen_producto

        self._stock_inicial(430)
        self._venta(6, 15, 5, 30)
        conteo = self._conteo(425, 15, 3, 20)
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        resolver_discrepancia(discrepancia, discrepancia.ajuste_sugerido, self.ruth, "Faltante real")

        resumen = resumen_producto(self.gallo, self.hoy, self.hoy)
        self.assertEqual(resumen["unidades_ajuste"], -5)
        self.assertEqual(resumen["stock_teorico_al_cierre"], 419, "430 − 6 − 5 = 419.")
        self.assertIn(
            "unidades_ajuste", [clave for _, clave in COLUMNAS_RESUMEN],
            "El Excel no incluye la columna de ajustes, así que el faltante queda invisible.",
        )

    def test_un_conteo_registrado_sin_conexion_conserva_su_hora_real(self):
        """
        El caso del escenario 1, por el camino que de verdad lo produce.

        La cola serializa la instancia ANTES de guardarla, así que en ese
        momento ocurrido_en todavía es None. Sin fijarlo ahí, el payload
        viaja sin instante y la fila que se crea al sincronizar se sella
        con la hora de SINCRONIZACIÓN — el bug original de vuelta, y solo
        en el camino offline, que es justo donde se manifiesta.
        """
        from django.utils import timezone as tz

        from .models import PendienteSincronizacion
        from .offline import encolar_pendiente

        self._stock_inicial(430)

        conteo_sin_guardar = ConteoFisico(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=425,
            registrado_por=self.michelle,
        )
        self.assertIsNone(conteo_sin_guardar.ocurrido_en, "Antes de guardar no hay instante.")

        antes = tz.now()
        encolar_pendiente(conteo_sin_guardar)   # el código real, sin simular
        despues = tz.now()

        payload = PendienteSincronizacion.objects.using("local_disco").get(
            uuid=conteo_sin_guardar.uuid
        ).payload
        self.assertIsNotNone(
            payload.get("ocurrido_en"),
            "El instante no viaja en la cola: la fila remota se sellaría con la hora de "
            "sincronización, que es exactamente el bug del escenario 1.",
        )
        instante = _deserializar_valor_prueba(payload["ocurrido_en"])
        self.assertTrue(
            antes <= instante <= despues,
            "El instante encolado no es el momento en que la persona registró el conteo.",
        )

        # Y al reconstruirlo del otro lado, se respeta. Se deserializa
        # el payload entero igual que en la sincronización real.
        from .offline import _deserializar_valor

        kwargs = {clave: _deserializar_valor(valor) for clave, valor in payload.items()}
        recreado = ConteoFisico.objects.create(**kwargs)
        self.assertEqual(
            recreado.ocurrido_en, instante,
            "Al sincronizar se perdió la hora real y quedó la de sincronización.",
        )
        discrepancia = DiscrepanciaInventario.objects.get(conteo=recreado)
        self.assertEqual(discrepancia.teorico_al_conteo, 430)

    def test_un_faltante_resta_de_la_ganancia_y_un_sobrante_no_suma(self):
        """
        Prompt 34b: los dos ajustes se tratan distinto a propósito.

        Un faltante es inventario real que ya no está, con su costo — pesa
        igual que una merma. Un sobrante no es una venta realizada: darlo
        por ganancia inflaría el período por algo que solo significa que
        el registro estaba mal. Se ve en su columna, no en la ganancia.
        """
        from .services import resumen_general, resumen_producto

        self._stock_inicial(430)                       # 430 a Q6.50
        self._venta(10, 12, 0)                         # 10 a Q15.00
        ganancia_sin_ajustes = resumen_producto(self.gallo, self.hoy, self.hoy)["ganancia_neta"]
        self.assertEqual(ganancia_sin_ajustes, Decimal("85.00"), "10 × (15 − 6.50) = 85.00")

        # --- Faltante de 5 unidades ---
        conteo_faltante = self._conteo(415, 14, 0)     # teórico 420 → −5
        faltante = DiscrepanciaInventario.objects.get(conteo=conteo_faltante)
        self.assertEqual(faltante.diferencia, -5)
        resolver_discrepancia(faltante, faltante.ajuste_sugerido, self.ruth, "Faltante real")

        con_faltante = resumen_producto(self.gallo, self.hoy, self.hoy)
        self.assertEqual(con_faltante["unidades_ajuste"], -5)
        self.assertEqual(
            con_faltante["perdida_por_ajuste"], Decimal("32.50"), "5 × Q6.50 = Q32.50"
        )
        self.assertEqual(
            con_faltante["ganancia_neta"], Decimal("52.50"),
            "85.00 − 32.50: el faltante tiene que restar igual que una merma.",
        )

        # --- Sobrante de 5 unidades, sobre el mismo producto ---
        conteo_sobrante = self._conteo(420, 16, 0)     # teórico 415 → +5
        sobrante = DiscrepanciaInventario.objects.get(conteo=conteo_sobrante)
        self.assertEqual(sobrante.diferencia, 5)
        resolver_discrepancia(sobrante, sobrante.ajuste_sugerido, self.ruth, "Sobrante confirmado")

        con_ambos = resumen_producto(self.gallo, self.hoy, self.hoy)
        self.assertEqual(
            con_ambos["unidades_ajuste"], 0, "−5 y +5 se cancelan en el conteo de unidades."
        )
        self.assertEqual(
            con_ambos["perdida_por_ajuste"], Decimal("32.50"),
            "El sobrante NO puede cancelar la pérdida del faltante: son cosas distintas.",
        )
        self.assertEqual(
            con_ambos["ganancia_neta"], Decimal("52.50"),
            "El sobrante no suma nada: la ganancia se queda igual que con solo el faltante.",
        )

        # --- Y la ruta rápida del reporte tiene que dar lo mismo ---
        general = resumen_general(self.hoy, self.hoy, productos=[self.gallo])
        fila = general["productos"][0]
        self.assertEqual(fila["perdida_por_ajuste"], Decimal("32.50"))
        self.assertEqual(fila["ganancia_neta"], Decimal("52.50"))


class HistorialFiltrosTests(TestCase):
    """
    Prompt 35: filtros de Historial por tipo de movimiento, usuario,
    producto y fecha, combinables entre sí con AND, sobre el orden
    cronológico REAL (ocurrido_en, no creado_en/orden de llegada).
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        from django.utils import timezone as tz

        self.hoy = date(2026, 8, 31)
        self.tz = tz
        categoria = Categoria.objects.create(nombre="Cervezas 35")
        self.gallo = Producto.objects.create(
            nombre="Gallo 35", categoria=categoria, precio_venta_actual=Decimal("15.00")
        )
        self.corona = Producto.objects.create(
            nombre="Corona 35", categoria=categoria, precio_venta_actual=Decimal("18.00")
        )
        self.ruth = User.objects.create_user(username="ruth35", password="x")
        self.michelle = User.objects.create_user(username="michelle35", password="x")
        grupo_admin, _ = Group.objects.get_or_create(name="admin")
        self.ruth.groups.add(grupo_admin)

    def _instante(self, hora, minuto, segundo=0, dia=None):
        dia = dia or self.hoy
        ingenuo = datetime(dia.year, dia.month, dia.day, hora, minuto, segundo)
        return self.tz.make_aware(ingenuo, self.tz.get_current_timezone())

    def _venta(self, producto, cantidad, usuario, hora, minuto):
        return MovimientoSalida.objects.create(
            producto=producto, fecha=self.hoy, tipo="venta", cantidad=cantidad,
            precio_venta_unitario=Decimal("15.00"), registrado_por=usuario,
            ocurrido_en=self._instante(hora, minuto),
        )

    # --- Cada criterio por separado -------------------------------------

    def test_filtro_por_tipo_venta_no_trae_otros_tipos(self):
        from .services import TIPO_VENTA, movimientos_periodo

        self._venta(self.gallo, 5, self.ruth, 10, 0)
        MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="merma", cantidad=1,
            costo_unitario_snapshot=Decimal("6.50"), registrado_por=self.ruth,
            ocurrido_en=self._instante(11, 0),
        )
        LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad=100, costo_unitario=Decimal("6.50"),
            registrado_por=self.ruth, ocurrido_en=self._instante(9, 0),
        )

        filas = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_VENTA)

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["tipo"], "Venta")

    def test_filtro_por_usuario_solo(self):
        """'todos los movimientos de Ruth en la última semana'."""
        from .services import movimientos_periodo

        self._venta(self.gallo, 5, self.ruth, 10, 0)
        self._venta(self.gallo, 3, self.michelle, 11, 0)

        filas = movimientos_periodo(self.hoy, self.hoy, usuario_id=self.ruth.pk)

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["usuario"], "ruth35")

    # --- Combinación con AND ---------------------------------------------

    def test_tipo_y_usuario_combinados_con_and(self):
        """'ventas realizadas por Michelle': tipo=venta Y usuario=Michelle."""
        from .services import TIPO_VENTA, movimientos_periodo

        self._venta(self.gallo, 5, self.ruth, 10, 0)       # venta, pero de Ruth
        self._venta(self.gallo, 3, self.michelle, 11, 0)    # venta de Michelle -> coincide
        MovimientoSalida.objects.create(                    # merma de Michelle, pero no es venta
            producto=self.gallo, fecha=self.hoy, tipo="merma", cantidad=1,
            costo_unitario_snapshot=Decimal("6.50"), registrado_por=self.michelle,
            ocurrido_en=self._instante(12, 0),
        )

        filas = movimientos_periodo(
            self.hoy, self.hoy, tipo_movimiento=TIPO_VENTA, usuario_id=self.michelle.pk
        )

        self.assertEqual(len(filas), 1, "AND, no OR: ni la venta de Ruth ni la merma de Michelle deben aparecer.")
        self.assertEqual(filas[0]["usuario"], "michelle35")
        self.assertEqual(filas[0]["cantidad"], 3)

    def test_tipo_ajuste_y_producto_combinados(self):
        """'ajustes realizados sobre el producto Corona'."""
        from .discrepancias import resolver_discrepancia
        from .services import TIPO_AJUSTE_FALTANTE, movimientos_periodo

        LoteCompra.objects.create(
            producto=self.corona, fecha=self.hoy, cantidad=100, costo_unitario=Decimal("8.00"),
            registrado_por=self.ruth, ocurrido_en=self._instante(8, 0),
        )
        LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad=100, costo_unitario=Decimal("6.50"),
            registrado_por=self.ruth, ocurrido_en=self._instante(8, 0),
        )
        conteo_corona = ConteoFisico.objects.create(
            producto=self.corona, fecha=self.hoy, cantidad_contada=95,
            registrado_por=self.ruth, ocurrido_en=self._instante(14, 0),
        )
        conteo_gallo = ConteoFisico.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=90,
            registrado_por=self.ruth, ocurrido_en=self._instante(14, 0),
        )
        resolver_discrepancia(
            DiscrepanciaInventario.objects.get(conteo=conteo_corona),
            5, self.ruth, "faltante Corona confirmado",
        )
        resolver_discrepancia(
            DiscrepanciaInventario.objects.get(conteo=conteo_gallo),
            10, self.ruth, "faltante Gallo confirmado",
        )

        filas = movimientos_periodo(
            self.hoy, self.hoy, productos=[self.corona], tipo_movimiento=TIPO_AJUSTE_FALTANTE,
        )

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["producto"], self.corona)
        self.assertEqual(filas[0]["cantidad"], 5)

    # --- La distinción que el prompt pide explícitamente ------------------

    def test_ajuste_confirmado_y_discrepancia_pendiente_son_tipos_distintos(self):
        from .discrepancias import resolver_discrepancia
        from .services import TIPO_AJUSTE_FALTANTE, TIPO_DISCREPANCIA_PENDIENTE, movimientos_periodo

        LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad=430, costo_unitario=Decimal("6.50"),
            registrado_por=self.ruth, ocurrido_en=self._instante(8, 0),
        )
        conteo_resuelto = ConteoFisico.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=425,
            registrado_por=self.ruth, ocurrido_en=self._instante(10, 0),
        )
        resolver_discrepancia(
            DiscrepanciaInventario.objects.get(conteo=conteo_resuelto), 5, self.ruth, "confirmado",
        )
        ConteoFisico.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=400,
            registrado_por=self.ruth, ocurrido_en=self._instante(16, 0),
        )  # queda pendiente, nadie la resolvió

        solo_ajustes = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_AJUSTE_FALTANTE)
        solo_pendientes = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_DISCREPANCIA_PENDIENTE)

        self.assertEqual(len(solo_ajustes), 1, "El filtro de ajuste no debe traer la discrepancia pendiente.")
        self.assertEqual(solo_ajustes[0]["tipo_registro"], "MovimientoSalida")
        self.assertEqual(len(solo_pendientes), 1, "El filtro de pendientes no debe traer el ajuste ya confirmado.")
        self.assertEqual(solo_pendientes[0]["tipo_registro"], "ConteoFisico")
        self.assertEqual(solo_pendientes[0]["cantidad"], 400)

    # --- Orden cronológico real, no de llegada -----------------------------

    def test_orden_usa_ocurrido_en_no_creado_en(self):
        """
        Un movimiento con ocurrido_en TEMPRANO pero creado_en (inserción
        en la base) TARDÍO —el caso de la cola offline sincronizando
        tarde— debe aparecer en su lugar cronológico real, no al final.
        """
        from .services import movimientos_periodo

        temprano = MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=1,
            precio_venta_unitario=Decimal("15.00"), registrado_por=self.ruth,
            ocurrido_en=self._instante(8, 0),
        )
        tarde = MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=2,
            precio_venta_unitario=Decimal("15.00"), registrado_por=self.ruth,
            ocurrido_en=self._instante(20, 0),
        )
        # creado_en es auto_now_add: se fija solo, en el orden de creación
        # real de estas dos filas en la prueba (temprano antes que tarde).
        # Si el sort usara creado_en, "temprano" seguiría saliendo primero
        # en orden ascendente — así que se fuerza lo contrario a mano para
        # que la prueba distinga de verdad cuál campo se está usando.
        MovimientoSalida.objects.filter(pk=temprano.pk).update(
            creado_en=self._instante(23, 0)
        )
        MovimientoSalida.objects.filter(pk=tarde.pk).update(
            creado_en=self._instante(1, 0)
        )

        filas = movimientos_periodo(self.hoy, self.hoy, descendente=False)

        self.assertEqual(
            [f["cantidad"] for f in filas], [1, 2],
            "Con ocurrido_en, 'temprano' (08:00) va antes que 'tarde' (20:00) sin importar "
            "en qué orden se insertaron en la base.",
        )

    # --- Paginación ---------------------------------------------------------

    def test_paginacion_no_pierde_filas_y_respeta_el_filtro(self):
        from django.core.paginator import Paginator

        from .services import TIPO_VENTA, movimientos_periodo

        for i in range(75):
            self._venta(self.gallo, 1, self.ruth, 6 + (i // 60), i % 60)
        # 5 mermas de más, para confirmar que el filtro de tipo se aplicó
        # ANTES de paginar y no se cuelan en ninguna página.
        for i in range(5):
            MovimientoSalida.objects.create(
                producto=self.gallo, fecha=self.hoy, tipo="merma", cantidad=1,
                costo_unitario_snapshot=Decimal("6.50"), registrado_por=self.ruth,
                ocurrido_en=self._instante(5, i),
            )

        filas = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_VENTA)
        self.assertEqual(len(filas), 75)

        paginador = Paginator(filas, 50)
        self.assertEqual(paginador.num_pages, 2)
        pagina_1 = paginador.get_page(1)
        pagina_2 = paginador.get_page(2)
        self.assertEqual(len(pagina_1.object_list), 50)
        self.assertEqual(len(pagina_2.object_list), 25)
        self.assertTrue(all(f["tipo"] == "Venta" for f in pagina_1.object_list))
        self.assertTrue(all(f["tipo"] == "Venta" for f in pagina_2.object_list))
        # Ninguna fila se repite ni se pierde entre las dos páginas.
        ids_pagina_1 = {f["registro_id"] for f in pagina_1.object_list}
        ids_pagina_2 = {f["registro_id"] for f in pagina_2.object_list}
        self.assertEqual(len(ids_pagina_1 & ids_pagina_2), 0)
        self.assertEqual(len(ids_pagina_1 | ids_pagina_2), 75)

    def test_vista_historial_pagina_y_muestra_el_total_filtrado(self):
        for i in range(60):
            self._venta(self.gallo, 1, self.ruth, 6, i % 60 if i < 60 else 0)

        self.client.force_login(self.ruth)
        respuesta = self.client.get(reverse("historial"))

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(len(respuesta.context["filas"]), 50, "La página 1 debe traer 50, no las 60.")
        self.assertEqual(respuesta.context["total_filas"], 60, "El contador debe ser el TOTAL filtrado, no el de la página.")
        self.assertContains(respuesta, "60 movimientos")
        self.assertContains(respuesta, "Página 1 de 2")

        respuesta_2 = self.client.get(reverse("historial"), {"page": 2})
        self.assertEqual(len(respuesta_2.context["filas"]), 10)

    # --- Los mismos filtros, sin conexión ------------------------------------

    def test_filtros_tambien_aplican_sin_conexion(self):
        from .models import CredencialOfflineCache, MovimientoHistorialCache
        from .offline import historial_offline, refrescar_catalogo_cache

        refrescar_catalogo_cache()
        CredencialOfflineCache.objects.using("local_disco").update_or_create(
            username="ruth35", defaults={
                "user_id": self.ruth.pk, "password_hash": "x", "rol": "admin",
                "is_active": True, "is_staff": True, "is_superuser": True,
            },
        )
        CredencialOfflineCache.objects.using("local_disco").update_or_create(
            username="michelle35", defaults={
                "user_id": self.michelle.pk, "password_hash": "x", "rol": "auditor",
                "is_active": True, "is_staff": False, "is_superuser": False,
            },
        )
        MovimientoHistorialCache.objects.using("local_disco").all().delete()
        venta_ruth = self._venta(self.gallo, 5, self.ruth, 10, 0)
        venta_michelle = self._venta(self.gallo, 3, self.michelle, 11, 0)
        from .offline import refrescar_historial_cache

        refrescar_historial_cache()

        from .services import TIPO_VENTA

        filas = historial_offline(
            self.hoy, self.hoy, tipo_movimiento=TIPO_VENTA, usuario_id=self.michelle.pk,
        )

        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["usuario"], "michelle35")
        self.assertEqual(filas[0]["cantidad"], 3)


class LoginConConexionRealTests(TransactionTestCase):
    """
    EL SÍNTOMA EXACTO DEL PROMPT 33D: en Windows, con internet activo y
    funcionando, el login se resolvía contra la caché local en vez de
    contra Neon.

    TransactionTestCase y no TestCase a propósito: TestCase envuelve cada
    prueba en una transacción, así que la conexión queda ABIERTA y
    ensure_connection() nunca llama a get_new_connection() — justo donde
    vive el corto que causaba el bug. Con la conexión abierta esta prueba
    pasaría siempre, incluso con el bug presente.
    """

    databases = {"default", "local_disco"}

    def test_login_no_cae_al_cache_cuando_hay_conexion_pero_el_sondeo_es_lento(self):
        import threading
        import time

        from django.contrib.auth.hashers import make_password
        from django.db import connections

        from . import offline
        from .models import CredencialOfflineCache
        from .offline import BackendConRespaldoOffline, guardar_credencial_offline

        if not _es_postgres(connections["default"].settings_dict["ENGINE"]):
            self.skipTest("El corto vive en el backend de Postgres; con SQLite no aplica.")

        CLAVE_REAL = "clave-real-33d"
        usuario = User.objects.create_user(username="usuario33d", password=CLAVE_REAL)
        grupo, _ = Group.objects.get_or_create(name="admin")
        usuario.groups.add(grupo)
        guardar_credencial_offline(usuario, "admin")

        # La caché local queda con OTRA contraseña. Así los dos caminos son
        # distinguibles sin ambigüedad: si el login valida contra Neon
        # devuelve el usuario; si cae a la caché, el hash no coincide y
        # devuelve None. No hay forma de confundir cuál se usó.
        CredencialOfflineCache.objects.using("local_disco").filter(
            username="usuario33d"
        ).update(password_hash=make_password("otra-clave-que-ya-no-vale"))

        # Un sondeo SANO pero lento en curso — exactamente lo que pasa en
        # la VM de Windows, donde el handshake tarda más que en Mac.
        original = offline._reciclar_conexion_remota

        def reciclar_lento():
            time.sleep(3.0)

        offline._reciclar_conexion_remota = reciclar_lento
        self.addCleanup(setattr, offline, "_reciclar_conexion_remota", original)
        self.addCleanup(offline.reiniciar_cache_conexion)
        offline.reiniciar_cache_conexion()

        hilo = threading.Thread(target=offline.hay_conexion)
        hilo.start()
        time.sleep(2.0)  # el sondeo lleva más de los 2s que antes bastaban

        # Conexión cerrada: el login TIENE que abrir una nueva y pasar por
        # get_new_connection(), que es donde estaba el corto.
        connections["default"].close()

        backend = BackendConRespaldoOffline()
        autenticado = backend.authenticate(None, username="usuario33d", password=CLAVE_REAL)

        hilo.join(timeout=10)

        self.assertIsNotNone(
            autenticado,
            "El login se resolvió contra la CACHÉ LOCAL teniendo conexión: la caché tenía "
            "otra contraseña, así que devolver None prueba que nunca se consultó Neon. "
            "Esa es exactamente la regresión del prompt 33d.",
        )
        self.assertEqual(autenticado.username, "usuario33d")


class NotaConflictoMenorTests(TestCase):
    """
    Prompt 34 punto 1, completado en el prompt 37.

    El diseño del 34 prometía que un conflicto de orden MENOR —uno que no
    cambia ningún resultado— dejara una nota informativa en vez de
    bloquear, y esa pieza nunca se construyó: esos casos no hacían
    absolutamente nada.

    Al completarla se descubrió por qué había quedado a medias: la rama
    que el 34 dejó preparada dentro de marcar_afectadas_por() ("si el
    teórico recalculado da igual, no hagas nada") es INALCANZABLE.
    Instrumentada, no se ejecutó ni una vez en las 75 pruebas de la
    suite, y estructuralmente no puede: cualquier movimiento que caiga en
    el pasado de una discrepancia pendiente siempre entra en el filtro de
    stock_teorico(), así que el número SIEMPRE cambia. El conflicto menor
    se detecta donde de verdad ocurre — cuando un movimiento sincroniza
    tarde y, al colocarlo en su lugar cronológico, no altera ninguna
    diferencia pendiente.
    """

    databases = {"default", "local_disco"}

    def setUp(self):
        from django.utils import timezone as tz

        self.tz = tz
        self.hoy = date(2026, 8, 31)
        categoria = Categoria.objects.create(nombre="Cervezas 37")
        self.gallo = Producto.objects.create(
            nombre="Gallo 37", categoria=categoria, precio_venta_actual=Decimal("15.00")
        )
        self.ruth = User.objects.create_user(username="ruth37", password="x")

    def _instante(self, hora, minuto=0):
        return self.tz.make_aware(
            datetime(self.hoy.year, self.hoy.month, self.hoy.day, hora, minuto),
            self.tz.get_current_timezone(),
        )

    @contextmanager
    def _montando_el_escenario(self):
        """
        Silencia la detección de desfase mientras se arma el escenario.

        Hace falta por cómo funciona el reloj en una prueba, no por la
        lógica: aquí se fija ocurrido_en en una fecha concreta del pasado
        mientras creado_en es auto_now_add y queda en "ahora", así que
        TODO objeto de andamiaje parecería haber sincronizado con horas de
        desfase y generaría notas que en producción nunca existirían (una
        escritura en línea real pone ocurrido_en = ahora dentro de
        save(), y el desfase es de milisegundos). Con esto, la única nota
        que puede aparecer es la del movimiento que la prueba SÍ está
        midiendo.
        """
        from . import discrepancias

        original = discrepancias.SEGUNDOS_DESFASE_PARA_NOTA
        discrepancias.SEGUNDOS_DESFASE_PARA_NOTA = 10 ** 9
        try:
            yield
        finally:
            discrepancias.SEGUNDOS_DESFASE_PARA_NOTA = original

    def _lote(self, cantidad, hora):
        return LoteCompra.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad=cantidad,
            costo_unitario=Decimal("6.50"), registrado_por=self.ruth,
            ocurrido_en=self._instante(hora),
        )

    def _conteo(self, cantidad, hora):
        return ConteoFisico.objects.create(
            producto=self.gallo, fecha=self.hoy, cantidad_contada=cantidad,
            registrado_por=self.ruth, ocurrido_en=self._instante(hora),
        )

    def _venta_que_sincronizo_tarde(self, cantidad, hora, minutos_de_desfase):
        """
        Una venta registrada sin conexión que llegó tarde: su instante
        real es anterior al momento en que la base la insertó. Ese desfase
        es lo que la app ve como "esto llegó fuera de orden".

        creado_en es auto_now_add, así que se fija después de crear y la
        señal se dispara a mano — es la única forma de simular una llegada
        tardía sin esperarla de verdad.
        """
        from .discrepancias import marcar_afectadas_por, registrar_nota_conflicto_menor

        with self._montando_el_escenario():
            venta = MovimientoSalida.objects.create(
                producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=cantidad,
                precio_venta_unitario=Decimal("15.00"), registrado_por=self.ruth,
                ocurrido_en=self._instante(hora),
            )
        MovimientoSalida.objects.filter(pk=venta.pk).update(
            creado_en=self._instante(hora) + timedelta(minutes=minutos_de_desfase)
        )
        venta.refresh_from_db()
        afectadas = marcar_afectadas_por(
            self.gallo.pk, venta.fecha, venta.ocurrido_en,
            motivo="Movimiento que sincronizó tarde (prueba).",
        )
        registrar_nota_conflicto_menor(venta, afectadas)
        return venta

    def _escenario_con_conteo_cerrado(self):
        """
        Un conteo con faltante que YA se resolvió: su ajuste está aplicado
        y su resultado contabilizado. Es el escenario donde un movimiento
        que llega tarde produce un conflicto menor.
        """
        from .discrepancias import resolver_discrepancia
        from .models import DiscrepanciaInventario

        with self._montando_el_escenario():
            self._lote(500, hora=8)
            conteo = self._conteo(495, hora=18)
            discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
            resolver_discrepancia(discrepancia, 5, self.ruth, "Faltante confirmado.")
            discrepancia.refresh_from_db()
        self.assertEqual(discrepancia.estado, DiscrepanciaInventario.RESUELTA)
        return discrepancia

    def test_un_conflicto_menor_deja_nota_y_no_bloquea_nada(self):
        from .models import CorreccionHistorial

        discrepancia = self._escenario_con_conteo_cerrado()
        diferencia_antes = discrepancia.diferencia

        # Llega tarde y cae ANTES del conteo ya cerrado.
        venta = self._venta_que_sincronizo_tarde(3, hora=14, minutos_de_desfase=37)

        notas = CorreccionHistorial.objects.filter(accion=CorreccionHistorial.ACCION_NOTA)
        self.assertEqual(notas.count(), 1, "Un conflicto menor tiene que dejar constancia.")
        nota = notas.first()
        self.assertEqual(nota.registro_id, venta.pk)
        self.assertIsNone(nota.realizado_por, "La escribió el sistema, no una persona.")
        self.assertIn("37 minutos", nota.motivo)
        self.assertIn("no hace falta que nadie revise", nota.motivo)

        discrepancia.refresh_from_db()
        self.assertFalse(
            discrepancia.requiere_revision,
            "Un conflicto MENOR no puede bloquear ni pedir revisión de nadie.",
        )
        self.assertEqual(
            discrepancia.estado, discrepancia.RESUELTA,
            "El conteo cerrado sigue cerrado: la nota no reabre nada.",
        )
        self.assertEqual(discrepancia.diferencia, diferencia_antes)
        self.assertIsNone(
            discrepancia.diferencia_recalculada,
            "Nada se recalcula sobre un conteo cerrado: su ajuste ya está contabilizado.",
        )

    def test_un_movimiento_que_sincroniza_tarde_sin_alcanzar_nada_no_deja_nota(self):
        """
        El caso corriente y con diferencia el más frecuente: una venta
        offline sincroniza horas después, pero no cae en el pasado de
        ningún conteo. No hubo conflicto con nada, así que no hay nada que
        anotar.

        Esta prueba existe porque la primera versión de la nota SÍ se
        disparaba aquí, y eso duplicaba las filas del Historial: cada
        movimiento sincronizado desde la cola dejaba su propia nota.
        """
        from .models import CorreccionHistorial

        with self._montando_el_escenario():
            self._lote(500, hora=8)

        self._venta_que_sincronizo_tarde(3, hora=14, minutos_de_desfase=120)

        self.assertEqual(
            CorreccionHistorial.objects.filter(accion=CorreccionHistorial.ACCION_NOTA).count(), 0,
            "Sincronizar tarde por sí solo no es un conflicto: no puede llenar el Historial de notas.",
        )

    def test_un_conflicto_mayor_sigue_pidiendo_revision_y_no_deja_nota(self):
        from .models import CorreccionHistorial, DiscrepanciaInventario

        with self._montando_el_escenario():
            self._lote(500, hora=8)
            conteo = self._conteo(495, hora=18)
        discrepancia = DiscrepanciaInventario.objects.get(conteo=conteo)
        self.assertEqual(discrepancia.diferencia, -5)

        # Movimiento tardío que SÍ cae antes del conteo: cambia su número.
        self._venta_que_sincronizo_tarde(10, hora=12, minutos_de_desfase=90)

        discrepancia.refresh_from_db()
        self.assertTrue(
            discrepancia.requiere_revision,
            "Un conflicto MAYOR tiene que seguir marcándose para revisión humana.",
        )
        self.assertEqual(
            discrepancia.diferencia_recalculada, 5,
            "El teórico a las 18:00 pasa de 500 a 490 por la venta tardía de 10, así que "
            "la diferencia va de −5 a 495 − 490 = +5.",
        )
        self.assertEqual(
            CorreccionHistorial.objects.filter(accion=CorreccionHistorial.ACCION_NOTA).count(), 0,
            "Si hace falta revisión humana, la nota informativa sería ruido al lado.",
        )

    def test_un_movimiento_registrado_en_linea_no_genera_nota(self):
        from .models import CorreccionHistorial

        with self._montando_el_escenario():
            self._lote(500, hora=8)

        # Sin desfase: ocurrido_en y creado_en son el mismo instante, que
        # es lo que hace save() en una escritura en línea real.
        venta = MovimientoSalida.objects.create(
            producto=self.gallo, fecha=self.hoy, tipo="venta", cantidad=2,
            precio_venta_unitario=Decimal("15.00"), registrado_por=self.ruth,
        )
        self.assertLess(
            (venta.creado_en - venta.ocurrido_en).total_seconds(), 1,
            "save() debe poner ocurrido_en = ahora cuando no viene de la cola offline.",
        )
        self.assertEqual(
            CorreccionHistorial.objects.filter(accion=CorreccionHistorial.ACCION_NOTA).count(), 0,
            "Sin desfase no hubo conflicto de orden: no hay nada que anotar.",
        )

    def test_la_nota_se_ve_en_historial_y_el_filtro_de_tipo_la_distingue(self):
        from .services import TIPO_NOTA_AUTOMATICA, TIPO_VENTA, movimientos_periodo

        self._escenario_con_conteo_cerrado()
        self._venta_que_sincronizo_tarde(3, hora=14, minutos_de_desfase=45)

        solo_notas = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_NOTA_AUTOMATICA)
        self.assertEqual(len(solo_notas), 1)
        self.assertEqual(solo_notas[0]["tipo"], "Nota automática")
        self.assertEqual(solo_notas[0]["producto"], self.gallo)

        solo_ventas = movimientos_periodo(self.hoy, self.hoy, tipo_movimiento=TIPO_VENTA)
        self.assertTrue(
            all(f["tipo_codigo"] == TIPO_VENTA for f in solo_ventas),
            "El filtro de ventas no debe traer notas automáticas.",
        )

        todo = movimientos_periodo(self.hoy, self.hoy)
        tipos = {f["tipo_codigo"] for f in todo}
        self.assertIn(TIPO_NOTA_AUTOMATICA, tipos, "Sin filtro, la nota debe verse en el Historial.")
        self.assertIn(TIPO_VENTA, tipos)


class MatrizDeRolesTests(TestCase):
    """
    Cada URL de la app, contra cada uno de los tres roles (prompts 16, 25
    y 29; verificada de nuevo en el 37 punto 1).

    Las pruebas de rol que existían eran puntuales —cuatro casos sueltos—
    y por eso las vistas de discrepancias del prompt 34 llegaron a
    producción sin ninguna. Esta clase fija la matriz COMPLETA y, además,
    falla si aparece una ruta nueva que no esté en la tabla: así una vista
    añadida más adelante no puede quedarse sin comprobación de permisos
    por olvido.

    Se comprueba "no es 403" en vez de "es 200" a propósito: varias rutas
    son POST-only (405) o redirigen (302), y lo que esta prueba defiende
    es el permiso, no el cuerpo de la respuesta.
    """

    #: nombre de ruta -> (kwargs, roles que SÍ pueden entrar)
    TODOS = ("admin", "auditor", "vendedor")
    MATRIZ = {
        "home": ({}, TODOS),
        "instrucciones": ({}, TODOS),
        "estado_sincronizacion": ({}, TODOS),
        "movimientosalida_create": ({}, TODOS),
        "categoria_list": ({}, ("admin", "auditor")),
        "categoria_create": ({}, ("admin", "auditor")),
        "categoria_update": ({"pk": 1}, ("admin", "auditor")),
        "categoria_toggle": ({"pk": 1}, ("admin", "auditor")),
        "producto_list": ({}, ("admin", "auditor")),
        "producto_create": ({}, ("admin", "auditor")),
        "producto_update": ({"pk": 1}, ("admin", "auditor")),
        "producto_toggle": ({"pk": 1}, ("admin", "auditor")),
        "lotecompra_create": ({}, ("admin", "auditor")),
        "conteofisico_create": ({}, ("admin", "auditor")),
        "conteofisico_detail": ({"pk": 1}, ("admin", "auditor")),
        "conteofisico_generar_ajuste": ({"pk": 1}, ("admin", "auditor")),
        "discrepancias": ({}, ("admin", "auditor")),
        "discrepancia_resolver": ({"pk": 1}, ("admin", "auditor")),
        "historial": ({}, ("admin", "auditor")),
        "reportes": ({}, ("admin", "auditor")),
        "cola_sincronizacion": ({}, ("admin", "auditor")),
        "cola_sincronizacion_reintentar": ({"pendiente_id": 1}, ("admin", "auditor")),
        "cola_sincronizacion_reintentar_todos": ({}, ("admin", "auditor")),
        # Editar y borrar lo ya registrado es exclusivo de administrador:
        # ni el auditor puede (prompt 17).
        "correcciones_historial": ({}, ("admin",)),
        "lotecompra_correccion_editar": ({"pk": 1}, ("admin",)),
        "lotecompra_correccion_eliminar": ({"pk": 1}, ("admin",)),
        "movimientosalida_correccion_editar": ({"pk": 1}, ("admin",)),
        "movimientosalida_correccion_eliminar": ({"pk": 1}, ("admin",)),
        "conteofisico_correccion_editar": ({"pk": 1}, ("admin",)),
        "conteofisico_correccion_eliminar": ({"pk": 1}, ("admin",)),
    }

    def setUp(self):
        self.usuarios = {}
        for rol in self.TODOS:
            grupo, _ = Group.objects.get_or_create(name=rol)
            u = User.objects.create_user(username=f"{rol}_matriz", password="clave-matriz")
            u.groups.add(grupo)
            self.usuarios[rol] = u

    def test_la_tabla_cubre_todas_las_rutas_de_la_app(self):
        from . import urls as urls_inventario

        rutas = {p.name for p in urls_inventario.urlpatterns if p.name}
        sin_cubrir = rutas - set(self.MATRIZ)
        self.assertEqual(
            sin_cubrir, set(),
            "Rutas sin comprobación de permisos en MATRIZ. Toda vista nueva tiene que "
            "declarar aquí quién puede entrar antes de darse por terminada.",
        )
        sobrantes = set(self.MATRIZ) - rutas
        self.assertEqual(sobrantes, set(), "La tabla nombra rutas que ya no existen.")

    def test_cada_rol_solo_entra_donde_le_corresponde(self):
        from django.urls import reverse

        problemas = []
        for nombre, (kwargs, permitidos) in sorted(self.MATRIZ.items()):
            url = reverse(nombre, kwargs=kwargs)
            for rol in self.TODOS:
                self.client.force_login(self.usuarios[rol])
                codigo = self.client.get(url).status_code
                self.client.logout()
                if rol in permitidos and codigo == 403:
                    problemas.append(f"{nombre}: {rol} debería entrar y recibió 403")
                if rol not in permitidos and codigo != 403:
                    problemas.append(
                        f"{nombre}: {rol} NO debería entrar y recibió {codigo} en vez de 403"
                    )
        self.assertEqual(problemas, [], "\n".join(problemas))

    def test_sin_sesion_todo_redirige_al_login_y_nada_responde_contenido(self):
        from django.urls import reverse

        problemas = []
        for nombre, (kwargs, _) in sorted(self.MATRIZ.items()):
            url = reverse(nombre, kwargs=kwargs)
            respuesta = self.client.get(url)
            if respuesta.status_code != 302 or "/login" not in respuesta.headers.get("Location", ""):
                problemas.append(
                    f"{nombre}: sin sesión devolvió {respuesta.status_code} "
                    f"-> {respuesta.headers.get('Location', '(sin Location)')}"
                )
        self.assertEqual(problemas, [], "\n".join(problemas))


class PlantillasSanasTests(SimpleTestCase):
    """
    Errores de plantilla que Django no reporta: se ven en pantalla y ya.
    """

    def test_ningun_comentario_de_almohadilla_esta_partido_en_dos_lineas(self):
        """
        {# ... #} es de UNA sola línea en Django. Partido en dos, deja de
        ser un comentario y el texto se IMPRIME en la pantalla del
        usuario, sin ningún error ni aviso.

        Pasó de verdad dos veces: en el Historial (la nota automática del
        prompt 37 salía con el comentario del código pegado delante) y en
        la pantalla de "falta la configuración" del prompt 33, que es
        justo una de las que ve el usuario final cuando algo va mal. Para
        varias líneas va {% comment %}.
        """
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parent.parent
        partidos = []
        for archivo in sorted(raiz.rglob("*.html")):
            if {"venv", "staticfiles", "build", "dist"} & set(archivo.parts):
                continue
            for numero, linea in enumerate(archivo.read_text(errors="replace").splitlines(), 1):
                if "{#" in linea and "#}" not in linea.split("{#", 1)[1]:
                    partidos.append(f"{archivo.relative_to(raiz)}:{numero}")
        self.assertEqual(
            partidos, [],
            "Comentario {# #} partido en varias líneas: se imprimirá en pantalla. "
            "Usa {% comment %} ... {% endcomment %}. En: " + ", ".join(partidos),
        )


class SelectoresEnEspanolTests(TestCase):
    """
    Django 6 estrenó BLANK_CHOICE_LABEL ("- Select an option -") y todavía
    no viene traducida al español, así que cualquier desplegable de clave
    foránea que no fije empty_label sale en INGLÉS en una app que está
    entera en español. Pasaba en los cuatro formularios de uso diario
    (producto, entrada, salida, conteo) y en el filtro de Historial.
    """

    def test_ningun_desplegable_muestra_el_texto_en_ingles_de_django(self):
        from .forms import (
            ConteoFisicoForm, HistorialFiltroForm, LoteCompraForm,
            MovimientoSalidaForm, ProductoForm,
        )

        en_ingles = []
        for clase in (ProductoForm, LoteCompraForm, MovimientoSalidaForm,
                      ConteoFisicoForm, HistorialFiltroForm):
            for nombre, campo in clase().fields.items():
                etiqueta = getattr(campo, "empty_label", None)
                if etiqueta is None:
                    continue
                if "Select an option" in str(etiqueta) or str(etiqueta) == "---------":
                    en_ingles.append(f"{clase.__name__}.{nombre} -> {etiqueta!r}")
        self.assertEqual(
            en_ingles, [],
            "Desplegables con el texto por defecto de Django en vez de uno en español: "
            + ", ".join(en_ingles),
        )

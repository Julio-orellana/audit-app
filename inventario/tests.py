from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .forms import ConteoFisicoForm, LoteCompraForm, MovimientoSalidaForm
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
        Corte de red de verdad (no simulado con mocks): se apunta la
        conexión a un puerto local cerrado, que es lo mismo que ve la app
        cuando Neon deja de ser alcanzable.
        """
        from django.db import connections

        from .offline import hay_conexion

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
        # Y en cuanto vuelve la configuración buena, se reconecta sola.
        self.assertTrue(hay_conexion())

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

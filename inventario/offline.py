# inventario/offline.py
"""
Motor de sincronización offline (prompt 19, corregido a fondo en el 19b) —
patrón outbox.

Resuelve CUATRO problemas distintos, los cuatro confirmados con pruebas
reales antes de escribir esta implementación (no eran solo teoría):

1. Escrituras offline: registrar una venta/entrada/merma/conteo sin
   conexión debe guardarse de inmediato en una cola local y sincronizarse
   sola cuando vuelva internet — ver PendienteSincronizacion (models.py),
   ColaOfflineMixin (más abajo) y sincronizar_pendientes().
2. Iniciar sesión sin conexión: la app puede abrirse o reiniciarse
   justo durante el apagón, y si nadie puede entrar el modo offline no
   sirve de nada. Ver CredencialOfflineCache (models.py),
   autenticar_offline() y BackendConRespaldoOffline.
3. Continuidad de sesión offline: Django resolvía la sesión Y el usuario
   contra la base EN CADA REQUEST. Ahora la sesión vive en archivo local
   (SESSION_ENGINE en settings.py, ya no toca la base nunca) y el usuario
   se resuelve con BackendConRespaldoOffline.get_user(), que cae a la
   caché local cuando Neon no responde.
4. Catálogo offline: el dashboard básico y el formulario de venta
   necesitan poder listar productos sin tocar Neon. Ver
   refrescar_catalogo_cache() y el alias "local_disco" en settings.py
   (DATABASE_ROUTERS -> inventario/db_router.py).

UN SOLO alias local, "local_disco" (nunca habla con Neon): archivo SQLite
en BASE_DIR_ESCRIBIBLE. Guarda el catálogo cacheado, las credenciales
cacheadas y la cola de pendientes de CUALQUIER rol.

    Prompt 19b, punto 3 — cambio de decisión explícito: antes existía un
    segundo alias "local_memoria" (SQLite en RAM) para que la cola del
    vendedor se perdiera al cerrar el proceso. Esa decisión se revirtió:
    los tres roles (admin, auditor, vendedor) persisten su cola en el
    MISMO archivo local y sobreviven a un cierre forzado o un apagón.
    Ya no existe ningún caso especial de "solo memoria".

Edición/eliminación de historial (prompt 17) NUNCA pasa por esta cola —
ver CorreccionUpdateView/CorreccionDeleteView en views.py, que usan
RequiereConexionMixin (inventario/resiliencia.py) en vez de
ColaOfflineMixin.
"""
import logging
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password
from django.core.management import call_command
from django.db import close_old_connections, connections, transaction
from django.db.utils import InterfaceError, OperationalError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger("inventario.offline")

ERRORES_DE_CONEXION = (OperationalError, InterfaceError)

# Único alias local (ver docstring del módulo): mismo archivo para los
# tres roles desde el prompt 19b.
ALIAS_LOCAL = "local_disco"

# Marca en la sesión de que ESTE inicio de sesión se validó contra la
# caché local, no contra Neon — la usa ContinuidadSesionOfflineMiddleware
# para saber a quién avisarle si la revalidación posterior falla.
CLAVE_SESION_OFFLINE = "sesion_iniciada_sin_conexion"
CLAVE_AVISO_MOSTRADO = "aviso_revalidacion_mostrado"


# --- Preparación de la base local -------------------------------------------

_bases_locales_listas = False
_lock_preparacion = threading.Lock()

# manage.py test crea sus PROPIAS bases de prueba (incluida "local_disco",
# porque los TestCase la declaran en `databases`) y las migra solo — correr
# aquí un migrate contra la base real sería, además de inútil, tocar datos
# de verdad desde una prueba.
_MODO_PRUEBAS = "test" in sys.argv[:2]


def preparar_bases_locales():
    """
    Aplica las migraciones del alias local. Es idempotente y está
    protegida con un lock: se puede llamar desde cualquier hilo y las
    veces que sea. NUNCA depende de que "default" (Neon) esté disponible
    — es exactamente lo que permite que la app arranque sin conexión.
    """
    global _bases_locales_listas
    if _bases_locales_listas or _MODO_PRUEBAS:
        return
    with _lock_preparacion:
        if _bases_locales_listas:
            return
        call_command("migrate", database=ALIAS_LOCAL, run_syncdb=True, verbosity=0, interactive=False)
        _bases_locales_listas = True


# --- Conectividad ------------------------------------------------------------

def _reciclar_conexion_remota():
    """
    Cierra la conexión persistente a Neon si ya no sirve, para que el
    siguiente ensure_connection() abra una nueva en vez de reutilizar una
    muerta.

    ESTE ERA EL BUG RAÍZ DEL PROMPT 19B, PUNTO 2 (confirmado con una
    prueba reproducible, no por sospecha): ensure_connection() de Django
    solo conecta si `self.connection is None` — si el objeto conexión
    existe pero su socket ya murió (justo lo que pasa cuando se cae la
    red), NO revisa nada y regresa como si todo estuviera bien. El
    resultado era que hay_conexion() daba un FALSO POSITIVO para siempre
    y absolutamente TODA consulta del hilo de sincronización fallaba
    ciclo tras ciclo, sin recuperarse jamás — para los tres roles y las
    cuatro operaciones por igual. Las requests normales sí se recuperaban
    porque Django corre close_old_connections() al empezar cada request;
    el hilo de fondo no es una request y nunca pasaba por ahí.

    close_if_health_check_failed() de Django hace exactamente esta
    verificación, pero solo si CONN_HEALTH_CHECKS está activo — se
    replica aquí sin esa condición para que la detección funcione
    igual sea cual sea la configuración de la base.
    """
    conexion = connections["default"]
    if conexion.in_atomic_block:
        # Dentro de una transacción abierta NO se toca la conexión: cerrarla
        # aquí abortaría la transacción en curso y se perdería lo que ya
        # llevaba hecho. Y de todos modos no hace falta revisar nada — si hay
        # una transacción viva, la conexión evidentemente responde.
        #
        # Esto importa de verdad: hay_conexion() se llama desde dentro de
        # transaction.atomic() en varios caminos reales (las correcciones de
        # historial, generar_ajuste) y en TODA prueba con TestCase, que
        # envuelve cada test en una transacción.
        return
    conexion.close_if_unusable_or_obsolete()
    if conexion.connection is not None and not conexion.health_check_done:
        if not conexion.is_usable():
            conexion.close()
            if conexion.connection is not None:
                # close() no la soltó: la conexión quedó marcada como
                # "cerrada dentro de una transacción" en algún momento
                # anterior (pasa cuando la red se cae a media transacción)
                # y en ese estado close() no hace nada. Ya se verificó
                # arriba que ahora no hay transacción abierta, así que se
                # descarta a mano — si no, ensure_connection() vería un
                # objeto conexión que existe pero está muerto y volvería
                # el mismo falso positivo que este módulo corrige.
                conexion.connection = None
                conexion.closed_in_transaction = False
        # Marcarlo evita repetir el SELECT 1 en la misma request: Django
        # lo reinicia solo al empezar la siguiente (close_old_connections).
        conexion.health_check_done = True


def hay_conexion():
    """
    ¿Se puede hablar con Neon AHORA MISMO? Verifica de verdad la conexión
    (ver _reciclar_conexion_remota) y, si está muerta, la cierra para que
    el siguiente intento reconecte en vez de arrastrar el error.
    """
    conexion = connections["default"]
    try:
        _reciclar_conexion_remota()
        conexion.ensure_connection()
        return True
    except Exception:
        if not conexion.in_atomic_block:
            try:
                conexion.close()
            except Exception:
                pass
        return False


# --- Credenciales cacheadas y autenticación offline (prompt 19b, punto 1) ----

def guardar_credencial_offline(user, rol):
    """
    Guarda/actualiza la credencial local de un usuario que ACABA de
    autenticarse con éxito contra Neon.

    Nunca se guarda la contraseña en texto plano: se copia el hash que
    Django ya tiene en auth_user.password (PBKDF2 con sal), el mismo que
    usa check_password() para validar. Un atacante con acceso a este
    archivo no obtiene más de lo que obtendría con acceso a la tabla
    auth_user, y no puede deducir la contraseña de ahí.
    """
    preparar_bases_locales()
    from .models import CredencialOfflineCache

    try:
        CredencialOfflineCache.objects.using(ALIAS_LOCAL).update_or_create(
            username=user.username,
            defaults={
                "user_id": user.pk,
                "password_hash": user.password,
                "rol": rol,
                "is_active": user.is_active,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                # Se acaba de validar contra la base real: si había un
                # aviso pendiente de "tu contraseña cambió", ya no aplica.
                "aviso_password_cambiada": False,
            },
        )
    except Exception:
        logger.exception("No se pudo refrescar la caché local de credenciales.")


def _usuario_desde_cache(fila):
    """
    Construye un auth.User REAL (misma clase de siempre) pero sin
    guardarlo: viene de la caché local, no de una consulta a Neon.

    Es una instancia normal de User a propósito, no un objeto imitación:
    así funcionan sin trucos get_session_auth_hash() (que Django usa para
    validar la sesión en cada request), las plantillas, y cualquier código
    que espere un User. _rol_cache ya viene resuelto, así que rol_de() y
    _tiene_rol() (ver permisos.py) nunca intentan consultar .groups, que
    sí necesitaría la base.
    """
    from django.contrib.auth.models import User

    user = User(
        id=fila.user_id,
        username=fila.username,
        password=fila.password_hash,
        is_active=fila.is_active,
        is_staff=fila.is_staff,
        is_superuser=fila.is_superuser,
    )
    user._state.adding = False
    user._rol_cache = fila.rol
    user._resuelto_offline = True
    return user


def autenticar_offline(username, password):
    """
    Valida usuario+contraseña contra la caché local. Solo se llega aquí
    cuando la consulta normal contra Neon falló por conexión (ver
    BackendConRespaldoOffline) — nunca reemplaza la validación real
    mientras haya internet.

    Un usuario que nunca ha iniciado sesión en ESTA máquina con conexión
    no tiene fila en la caché y, correctamente, no puede entrar sin
    internet: no hay contra qué validarlo.
    """
    if not username or not password:
        return None
    preparar_bases_locales()
    from .models import CredencialOfflineCache

    try:
        fila = CredencialOfflineCache.objects.using(ALIAS_LOCAL).filter(username=username).first()
    except Exception:
        logger.exception("No se pudo leer la caché local de credenciales.")
        return None
    if fila is None or not fila.is_active:
        return None
    if not check_password(password, fila.password_hash):
        return None
    logger.warning("Inicio de sesión SIN CONEXIÓN validado contra la caché local: %s", username)
    return _usuario_desde_cache(fila)


def usuario_offline_por_id(user_id):
    """Resuelve request.user desde la caché local cuando Neon no responde."""
    preparar_bases_locales()
    from .models import CredencialOfflineCache

    try:
        fila = CredencialOfflineCache.objects.using(ALIAS_LOCAL).filter(user_id=user_id).first()
    except Exception:
        logger.exception("No se pudo leer la caché local de credenciales.")
        return None
    if fila is None or not fila.is_active:
        return None
    return _usuario_desde_cache(fila)


class BackendConRespaldoOffline(ModelBackend):
    """
    Único backend de autenticación de la app (AUTHENTICATION_BACKENDS en
    settings.py). Se comporta EXACTAMENTE como el ModelBackend de Django
    mientras haya conexión — misma validación de contraseña contra Neon,
    mismos permisos — y solo cae al respaldo local cuando la base no
    responde:

    - authenticate(): con conexión valida contra Neon y de paso refresca
      la credencial cacheada de este usuario en esta máquina; sin
      conexión valida contra esa caché (ver autenticar_offline).
    - get_user(): es lo que resuelve request.user en cada request. Con
      conexión, la consulta normal; sin conexión, la caché local — así
      una sesión ya iniciada sigue viva cuando se cae internet a media
      jornada, sin devolver al usuario a la pantalla de login.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            user = super().authenticate(request, username=username, password=password, **kwargs)
        except ERRORES_DE_CONEXION:
            return autenticar_offline(username or kwargs.get("username"), password)
        if user is not None:
            try:
                from .permisos import rol_de

                guardar_credencial_offline(user, rol_de(user))
            except Exception:
                # Que falle el refresco de la caché nunca debe impedir un
                # inicio de sesión que YA se validó correctamente.
                logger.exception("Inicio de sesión correcto pero no se pudo cachear la credencial.")
        return user

    def get_user(self, user_id):
        try:
            return super().get_user(user_id)
        except ERRORES_DE_CONEXION:
            return usuario_offline_por_id(user_id)


def _hash_de_sesion(password_hash):
    """
    Mismo cálculo que AbstractBaseUser.get_session_auth_hash(), pero a
    partir del hash guardado en la caché local en vez de una instancia de
    User — ver ContinuidadSesionOfflineMiddleware.
    """
    from django.utils.crypto import salted_hmac

    key_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"
    return salted_hmac(key_salt, password_hash, algorithm="sha256").hexdigest()


class ContinuidadSesionOfflineMiddleware:
    """
    Cierra el círculo de la revalidación pedida en el prompt 19b, punto 1:
    "cuando vuelva la conexión, revalida contra la base real en segundo
    plano (por si la contraseña cambió mientras tanto) SIN CERRARLE LA
    SESIÓN DE GOLPE al usuario, pero sí notifícalo".

    Quien detecta el cambio es refrescar_credenciales_cache() en el hilo
    de fondo, que marca aviso_password_cambiada en la fila local. Este
    middleware es la otra mitad: para una sesión que se inició SIN
    conexión y cuya contraseña cambió en Neon mientras tanto,

    1. reescribe el hash de sesión al valor actual, y
    2. avisa al usuario una sola vez.

    Sin el paso 1, Django detectaría por su cuenta que el hash de sesión
    ya no coincide con la contraseña actual y haría session.flush() — es
    decir, exactamente el cierre de sesión de golpe que el prompt pide
    evitar. Es una relajación consciente y muy acotada de esa protección
    de Django: aplica SOLO a una sesión marcada como iniciada sin
    conexión, una sola vez, y siempre acompañada del aviso visible.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            self._revisar(request)
        except Exception:
            # Nunca romper una request por el aviso.
            logger.exception("Error revisando la continuidad de la sesión offline.")
        return self.get_response(request)

    def _revisar(self, request):
        sesion = request.session
        if not sesion.get(CLAVE_SESION_OFFLINE) or sesion.get(CLAVE_AVISO_MOSTRADO):
            return
        user_id = sesion.get(SESSION_KEY)
        if not user_id:
            return
        from .models import CredencialOfflineCache

        fila = CredencialOfflineCache.objects.using(ALIAS_LOCAL).filter(user_id=user_id).first()
        if fila is None or not fila.aviso_password_cambiada:
            return
        sesion[HASH_SESSION_KEY] = _hash_de_sesion(fila.password_hash)
        sesion[CLAVE_AVISO_MOSTRADO] = True
        messages.warning(
            request,
            "Tu contraseña cambió mientras trabajabas sin conexión. Tu sesión sigue "
            "abierta y no se perdió nada, pero la próxima vez que inicies sesión "
            "tendrás que usar la contraseña nueva.",
        )


# --- Serialización de instancias --------------------------------------------

def _serializar_valor(valor):
    if isinstance(valor, Decimal):
        return {"__decimal__": str(valor)}
    # datetime ANTES que date (datetime es subclase de date) — perder esa
    # distinción aquí perdía la hora al reconstruir un campo datetime real
    # (ej. "creado_en" en el payload de MovimientoHistorialCache, prompt
    # 19c) y hacía fallar _deserializar_valor(), que intentaba parsear un
    # ISO-8601 con hora como si fuera solo fecha. _instancia_a_payload()
    # nunca pasó un datetime por aquí antes de esto (excluye "creado_en"
    # a propósito), así que el problema no se había manifestado.
    if isinstance(valor, datetime):
        return {"__datetime__": valor.isoformat()}
    if isinstance(valor, date):
        return {"__date__": valor.isoformat()}
    return valor


def _deserializar_valor(valor):
    if isinstance(valor, dict):
        if "__decimal__" in valor:
            return Decimal(valor["__decimal__"])
        if "__datetime__" in valor:
            return datetime.fromisoformat(valor["__datetime__"])
        if "__date__" in valor:
            return date.fromisoformat(valor["__date__"])
    return valor


def _instancia_a_payload(instance):
    """
    Dict JSON-serializable con los campos necesarios para recrear la
    instancia contra Neon vía Modelo.objects.get_or_create(uuid=...,
    defaults=payload) — usa field.attname (no field.name), así una FK
    como "producto" queda como "producto_id" directo, listo para pasar
    como kwarg. id/uuid/creado_en quedan afuera: id y creado_en los debe
    asignar la base al crear la fila remota; uuid ya viaja aparte como
    clave de idempotencia.
    """
    payload = {}
    for field in instance._meta.fields:
        if field.name in ("id", "uuid", "creado_en"):
            continue
        payload[field.attname] = _serializar_valor(field.value_from_object(instance))
    return payload


def _payload_a_kwargs(payload):
    return {clave: _deserializar_valor(valor) for clave, valor in payload.items()}


# --- Completar el snapshot de una venta offline -----------------------------

def completar_snapshot_offline(movimiento):
    """
    MovimientoSalida.save() calcula costo_unitario_snapshot/
    precio_venta_unitario consultando la base (costo_promedio() agrega
    LoteCompra) — sin conexión eso nunca se alcanza a ejecutar. Se
    completan aquí desde la copia local cacheada del catálogo, para que
    la venta que se encola quede con un snapshot "congelado" al momento
    real de la venta (no uno recalculado después, con el costo de otro
    día, cuando por fin sincronice — ver el docstring de services.py
    sobre por qué el snapshot nunca se recalcula).
    """
    from .models import Producto

    try:
        producto = Producto.objects.using(ALIAS_LOCAL).get(pk=movimiento.producto_id)
    except Producto.DoesNotExist:
        return
    if not movimiento.costo_unitario_snapshot and producto.costo_promedio_cache is not None:
        movimiento.costo_unitario_snapshot = producto.costo_promedio_cache
    if movimiento.tipo == "venta" and not movimiento.precio_venta_unitario:
        movimiento.precio_venta_unitario = producto.precio_venta_actual


# --- Validación de stock disponible al vender (prompt 19c, punto 5) ---------

def _mapa_producto_base_factor():
    """
    {producto_id: (producto_base_id o None, factor_equivalencia)} de TODO
    el catálogo cacheado en esta máquina — se usa para traducir un
    producto_id cualquiera (el de un pendiente en la cola) a su producto
    base y el factor con el que consume su stock, sin depender de si hay
    conexión (el catálogo local siempre existe una vez que refrescó al
    menos una vez).
    """
    from .models import Producto

    return {
        p["id"]: (p["producto_base_id"], p["factor_equivalencia"])
        for p in Producto.objects.using(ALIAS_LOCAL).values("id", "producto_base_id", "factor_equivalencia")
    }


def _ajuste_stock_pendiente_en_base(base_id):
    """
    Cuánto ya cambió el stock del producto BASE `base_id` por lo que hay
    AHORA MISMO en la cola de pendientes de esta máquina (encolado pero
    todavía no confirmado en Neon). Positivo = ya se consumió esa
    cantidad (hay que restarla del stock disponible); negativo = ya se
    sumó (una entrada o un ajuste por sobrante pendientes).

    Sin esto, dos ventas offline seguidas del mismo producto en la misma
    máquina podrían las dos "ver" el mismo stock cacheado (el que había
    al último refresco con conexión) y así permitir vender de más entre
    ellas — exactamente el escenario que el prompt 19c, punto 5, pide
    cubrir explícitamente.
    """
    from .models import PendienteSincronizacion

    try:
        pendientes = list(PendienteSincronizacion.objects.using(ALIAS_LOCAL).all())
    except Exception:
        return 0
    if not pendientes:
        return 0

    mapa = _mapa_producto_base_factor()
    total = 0
    for pendiente in pendientes:
        producto_id = pendiente.payload.get("producto_id")
        info = mapa.get(producto_id)
        if info is None:
            continue
        prod_base_id, factor = info
        if (prod_base_id or producto_id) != base_id:
            continue
        if pendiente.modelo == "LoteCompra":
            total -= pendiente.payload.get("cantidad") or 0
        elif pendiente.modelo == "MovimientoSalida":
            # cantidad ya viene con el signo real (ver models.py:
            # MovimientoSalida.cantidad) — positiva resta stock (venta,
            # merma, ajuste por faltante), negativa lo suma (ajuste por
            # sobrante). factor traduce unidades del derivado a
            # unidades base, igual que Producto.stock_teorico().
            total += (pendiente.payload.get("cantidad") or 0) * factor
        # ConteoFisico pendiente no mueve stock por sí solo (solo lo hace
        # el ajuste que se genera a partir de él, que sería su propio
        # MovimientoSalida pendiente aparte).
    return total


def stock_disponible_para_venta(producto):
    """
    Unidades de `producto` que se pueden vender AHORA MISMO, considerando
    tanto el stock real (Neon si hay conexión, la caché local si no) como
    lo que ya está reservado por la cola de pendientes de esta máquina
    (ver _ajuste_stock_pendiente_en_base) — para que MovimientoSalidaForm
    (forms.py) rechace una venta que dejaría el inventario en negativo,
    tanto online como offline, y sin basarse en un stock cacheado que ya
    quedó desactualizado por otra venta offline reciente en el mismo
    equipo.

    Para un producto derivado, el resultado ya está traducido con su
    factor_equivalencia — misma división entera que Producto.stock_teorico().
    """
    from .models import Producto

    base_id = producto.producto_base_id
    factor = producto.factor_equivalencia if base_id else 1

    if hay_conexion():
        base = producto if base_id is None else Producto.objects.using("default").get(pk=base_id)
        stock_base = base.stock_teorico()
    else:
        # Sin conexión no existen las tablas de movimientos en el alias
        # local (a propósito, ver db_router.py) — no hay forma de
        # recalcular stock_teorico() desde cero, se usa el último valor
        # calculado con conexión (ver refrescar_catalogo_cache).
        base_local = Producto.objects.using(ALIAS_LOCAL).get(pk=base_id or producto.pk)
        stock_base = base_local.stock_teorico_cache or 0

    stock_base_ajustado = stock_base - _ajuste_stock_pendiente_en_base(base_id or producto.pk)
    if base_id:
        return stock_base_ajustado // factor
    return stock_base_ajustado


# --- Cola de pendientes ------------------------------------------------------

def encolar_pendiente(instance):
    """
    Guarda la escritura en la cola local ANTES de intentarla contra Neon.
    Mismo archivo para los tres roles (prompt 19b, punto 3): sobrevive a
    un cierre forzado o un apagón.
    """
    preparar_bases_locales()
    from .models import PendienteSincronizacion

    PendienteSincronizacion.objects.using(ALIAS_LOCAL).update_or_create(
        uuid=instance.uuid,
        defaults={
            "modelo": type(instance).__name__,
            "payload": _instancia_a_payload(instance),
            "creado_en": timezone.now(),
        },
    )


def quitar_pendiente(uuid_valor):
    from .models import PendienteSincronizacion

    PendienteSincronizacion.objects.using(ALIAS_LOCAL).filter(uuid=uuid_valor).delete()


def contar_pendientes():
    from .models import PendienteSincronizacion

    try:
        preparar_bases_locales()
        return PendienteSincronizacion.objects.using(ALIAS_LOCAL).count()
    except Exception:
        return 0


class ColaOfflineMixin:
    """
    Mixin para las 3 CreateView de escritura relevante (LoteCompra,
    MovimientoSalida, ConteoFisico) — va ANTES de ReintentoEscrituraMixin
    en la lista de clases base, así que se ejecuta ALREDEDOR de sus
    reintentos.

    Dos caminos:

    - SIN conexión (detectado antes de intentar nada): se encola y se
      redirige de inmediato. No se intenta la escritura remota ni se
      pasa por los reintentos de ReintentoEscrituraMixin — sin eso, el
      usuario esperaba ~1 segundo (3 intentos con esperas) para algo que
      ya se sabía que iba a fallar. El prompt 19 es explícito en que la
      cola nunca debe hacer esperar al usuario.
    - CON conexión: se encola igual (primero la copia local, siempre) y
      se intenta la escritura real. Si funciona, se quita de la cola; si
      falla por conexión (se cayó la red justo en ese momento), se deja
      en la cola y se le confirma al usuario que quedó guardado.

    Debe ir DESPUÉS de ProteccionDobleSubmitMixin: así un reenvío
    duplicado detectado por el token nunca llega a encolarse dos veces.
    """

    def form_valid(self, form):
        encolar_pendiente(form.instance)
        if not hay_conexion():
            return self._respuesta_offline(form)
        try:
            respuesta = super().form_valid(form)
        except ERRORES_DE_CONEXION:
            return self._respuesta_offline(form)
        quitar_pendiente(form.instance.uuid)
        return respuesta

    def _respuesta_offline(self, form):
        self.object = form.instance
        messages.info(
            self.request,
            "Sin conexión: se guardó en este equipo y se sincronizará "
            "automáticamente en cuanto vuelva internet.",
        )
        return redirect(self.get_success_url_offline())

    def get_success_url_offline(self):
        """
        A dónde ir tras guardar sin conexión. NO se usa get_success_url():
        varias de estas vistas redirigen a Historial, que sin conexión
        está bloqueado — mandar ahí a alguien que acaba de registrar algo
        correctamente le mostraría una pantalla de "requiere conexión" y
        parecería que su registro falló. Inicio sí funciona sin conexión.
        """
        return reverse("home")


# --- Sincronización remota (idempotente) ------------------------------------

_MODELOS_SINCRONIZABLES = {}


def _clase_modelo(nombre):
    if not _MODELOS_SINCRONIZABLES:
        from .models import ConteoFisico, LoteCompra, MovimientoSalida

        _MODELOS_SINCRONIZABLES.update(
            LoteCompra=LoteCompra, MovimientoSalida=MovimientoSalida, ConteoFisico=ConteoFisico,
        )
    return _MODELOS_SINCRONIZABLES[nombre]


def _intentar_sincronizar_uno(pendiente):
    """
    Intenta subir UN pendiente a Neon. No decide qué hacer si falla (eso
    depende de quién llama: sincronizar_pendientes() corta el resto del
    lote ante un error de conexión, reintentar_uno_pendiente() —
    prompt 19c, punto 4 — no tiene ese problema porque ya es un solo
    elemento) — solo intenta, marca el intento fallido si corresponde, y
    reporta qué pasó.

    uuid como restricción única remota (LoteCompra/MovimientoSalida/
    ConteoFisico) es lo que hace esto idempotente: get_or_create() nunca
    duplica un registro cuyo uuid ya se confirmó en un intento anterior,
    aunque la respuesta de ESE intento nunca haya llegado localmente.

    Devuelve (sincronizado: bool, fue_error_de_conexion: bool).
    """
    try:
        Modelo = _clase_modelo(pendiente.modelo)
    except KeyError:
        logger.error("Pendiente con modelo desconocido (%s), se ignora.", pendiente.modelo)
        return False, False

    kwargs = _payload_a_kwargs(pendiente.payload)
    try:
        _, creado = Modelo.objects.using("default").get_or_create(uuid=pendiente.uuid, defaults=kwargs)
    except ERRORES_DE_CONEXION as error:
        _marcar_intento_fallido(pendiente, error)
        return False, True
    except Exception as error:
        # Un error que NO es de conexión (ej. una FK que ya no existe) no
        # se arregla reintentando para siempre igual — se deja registrado
        # (con el detalle en ultimo_error, visible en la cola de
        # sincronización) y se sigue con los demás.
        logger.exception("Error no recuperable sincronizando %s %s", pendiente.modelo, pendiente.uuid)
        _marcar_intento_fallido(pendiente, error)
        return False, False

    pendiente.delete(using=ALIAS_LOCAL)
    logger.info(
        "Sincronizado %s %s contra Neon (%s).",
        pendiente.modelo, pendiente.uuid, "creado" if creado else "ya existía, no se duplicó",
    )
    return True, False


def sincronizar_pendientes():
    """
    Sube cada pendiente de la cola local a Neon. La llama el hilo de
    fondo cada INTERVALO_SEGUNDOS, y también el botón "Reintentar todos"
    de la cola de sincronización (prompt 19c, punto 4).
    """
    preparar_bases_locales()
    from .models import PendienteSincronizacion

    total_sincronizados = 0
    try:
        pendientes = list(PendienteSincronizacion.objects.using(ALIAS_LOCAL).all())
    except Exception:
        logger.exception("No se pudo leer la cola de pendientes.")
        return 0

    for pendiente in pendientes:
        exito, fue_error_de_conexion = _intentar_sincronizar_uno(pendiente)
        if exito:
            total_sincronizados += 1
        elif fue_error_de_conexion:
            logger.warning(
                "Se cortó la conexión sincronizando %s %s — se reintenta en el próximo ciclo.",
                pendiente.modelo, pendiente.uuid,
            )
            # Si el primero falla por conexión, el resto de este ciclo
            # casi seguro también — no vale la pena seguir golpeando la
            # red, se reintenta todo junto en el próximo ciclo.
            break
    return total_sincronizados


def reintentar_uno_pendiente(pendiente_id):
    """
    Botón "Reintentar ahora" de un elemento puntual de la cola de
    sincronización (prompt 19c, punto 4) — para cuando la sincronización
    automática falló o no se ha disparado todavía. A diferencia del ciclo
    automático, un fallo aquí no corta nada más: es un solo elemento.

    Devuelve (sincronizado: bool, motivo: "conexion" | "error" | None).
    """
    preparar_bases_locales()
    from .models import PendienteSincronizacion

    try:
        pendiente = PendienteSincronizacion.objects.using(ALIAS_LOCAL).get(pk=pendiente_id)
    except PendienteSincronizacion.DoesNotExist:
        return False, None

    exito, fue_error_de_conexion = _intentar_sincronizar_uno(pendiente)
    if exito:
        return True, None
    return False, "conexion" if fue_error_de_conexion else "error"


def _marcar_intento_fallido(pendiente, error):
    pendiente.intentos += 1
    pendiente.ultimo_error = str(error)[:500]
    try:
        pendiente.save(using=ALIAS_LOCAL, update_fields=["intentos", "ultimo_error"])
    except Exception:
        pass


# --- Mostrar la cola de pendientes (prompt 19c, punto 4) --------------------

_ETIQUETAS_TIPO_PENDIENTE = {"LoteCompra": "Entrada", "ConteoFisico": "Conteo físico"}


def _tipo_mostrado_pendiente(pendiente):
    if pendiente.modelo != "MovimientoSalida":
        return _ETIQUETAS_TIPO_PENDIENTE.get(pendiente.modelo, pendiente.modelo)
    tipo = pendiente.payload.get("tipo")
    if tipo == "ajuste":
        cantidad = pendiente.payload.get("cantidad") or 0
        return "Ajuste (sobrante)" if cantidad < 0 else "Ajuste (faltante)"
    return {"venta": "Venta", "merma": "Merma"}.get(tipo, "Ajuste")


def listar_pendientes_para_mostrar():
    """
    Fila por pendiente para la pantalla "Cola de sincronización" (prompt
    19c, punto 4): tipo, producto, cantidad, fecha de creación local,
    intentos ya hechos y el último error (si algo falló antes) — todo
    resuelto desde la cola local y el catálogo cacheado, nunca desde
    Neon, así la pantalla funciona igual con o sin conexión.
    """
    from .models import PendienteSincronizacion, Producto

    try:
        pendientes = list(PendienteSincronizacion.objects.using(ALIAS_LOCAL).all())
    except Exception:
        return []
    if not pendientes:
        return []

    productos_por_id = {p.pk: p.nombre for p in Producto.objects.using(ALIAS_LOCAL).all()}

    filas = []
    for pendiente in pendientes:
        payload = pendiente.payload
        cantidad = payload.get("cantidad_contada", payload.get("cantidad"))
        filas.append(
            {
                "id": pendiente.pk,
                "tipo": _tipo_mostrado_pendiente(pendiente),
                "producto": productos_por_id.get(payload.get("producto_id"), "—"),
                "cantidad": abs(cantidad) if cantidad is not None else "—",
                "creado_en": pendiente.creado_en,
                "intentos": pendiente.intentos,
                "ultimo_error": pendiente.ultimo_error,
            }
        )
    return filas


# --- Catálogo cacheado -------------------------------------------------------

def refrescar_catalogo_cache():
    """
    Copia completa (borra y reinserta) de Categoria/Producto de "default"
    al alias local — solo tiene sentido llamarla cuando SÍ hay conexión;
    deja que la excepción se propague si no la hay, para que quien llama
    decida qué hacer.

    Se conserva el mismo pk que en Neon (necesario: la cola de pendientes
    guarda producto_id, tiene que apuntar al mismo producto real al
    sincronizar) y se calcula costo_promedio() y stock_teorico() de cada
    producto BASE aquí mismo, con conexión — son los dos únicos datos que
    la caché local no puede derivar sola sin tocar la base (agregan
    LoteCompra/MovimientoSalida, no son columnas). stock_teorico_cache
    (prompt 19c) es lo que permite validar "hay suficiente inventario"
    al registrar una venta sin conexión (ver stock_disponible_para_venta
    más abajo) — se usa MotorStockCosto (servicio ya existente, 3
    consultas fijas) en vez de Producto.stock_teorico() producto por
    producto, para no pagar una consulta extra por cada uno.
    """
    preparar_bases_locales()
    from .models import Categoria, Producto
    from .services import MotorStockCosto

    categorias = list(Categoria.objects.using("default").all())
    productos_base = list(Producto.objects.using("default").filter(producto_base__isnull=True))
    productos_derivados = list(Producto.objects.using("default").filter(producto_base__isnull=False))
    costos_base = {p.pk: p.costo_promedio() for p in productos_base}
    motor = MotorStockCosto()
    stocks_base = {p.pk: motor.stock_teorico(p.pk) for p in productos_base}

    with transaction.atomic(using=ALIAS_LOCAL):
        # DELETE crudo, no Producto.objects...delete(): el Collector de
        # borrado de Django revisa TODAS las relaciones inversas
        # (LoteCompra/MovimientoSalida/ConteoFisico apuntan a Producto
        # con on_delete=PROTECT) para decidir si puede borrar — y para
        # eso consulta esas tablas en el MISMO alias, que en el alias
        # local no existen a propósito (ver inventario/db_router.py). No
        # hay nada que proteger aquí: esta copia no tiene FKs de otras
        # tablas apuntándola en este alias, así que un DELETE directo es
        # seguro.
        with connections[ALIAS_LOCAL].cursor() as cursor:
            cursor.execute("DELETE FROM inventario_producto")
            cursor.execute("DELETE FROM inventario_categoria")
        for c in categorias:
            Categoria.objects.using(ALIAS_LOCAL).create(pk=c.pk, nombre=c.nombre, activo=c.activo)
        for p in productos_base:
            Producto.objects.using(ALIAS_LOCAL).create(
                pk=p.pk, nombre=p.nombre, categoria_id=p.categoria_id,
                precio_venta_actual=p.precio_venta_actual, activo=p.activo,
                producto_base_id=None, factor_equivalencia=p.factor_equivalencia,
                costo_promedio_cache=costos_base[p.pk],
                stock_teorico_cache=stocks_base[p.pk],
            )
        for p in productos_derivados:
            costo_base = costos_base.get(p.producto_base_id) or Decimal("0.00")
            Producto.objects.using(ALIAS_LOCAL).create(
                pk=p.pk, nombre=p.nombre, categoria_id=p.categoria_id,
                precio_venta_actual=p.precio_venta_actual, activo=p.activo,
                producto_base_id=p.producto_base_id, factor_equivalencia=p.factor_equivalencia,
                costo_promedio_cache=(costo_base * p.factor_equivalencia).quantize(Decimal("0.01")),
                # stock_teorico_cache de un derivado guarda el stock del
                # BASE (no dividido por factor) — ver el docstring del
                # campo en models.py: quien lo lea decide esa división.
                stock_teorico_cache=stocks_base.get(p.producto_base_id),
            )


# --- Revalidación de credenciales cacheadas ---------------------------------

def refrescar_credenciales_cache():
    """
    Revalida contra Neon las credenciales guardadas localmente (prompt
    19b, punto 1): rol, estado activo y sobre todo el hash de la
    contraseña, por si cambió mientras la máquina estaba sin conexión.

    No hace falta (ni sería aceptable) guardar la contraseña en claro
    para esto: basta comparar el hash cacheado contra el hash actual en
    Neon. Si difieren, la contraseña cambió — se actualiza la caché (para
    que el próximo inicio de sesión offline pida la nueva) y se marca
    aviso_password_cambiada, que ContinuidadSesionOfflineMiddleware
    convierte en un aviso visible sin cerrarle la sesión al usuario.
    """
    preparar_bases_locales()
    from django.contrib.auth.models import User

    from .models import CredencialOfflineCache
    from .permisos import rol_de

    filas = list(CredencialOfflineCache.objects.using(ALIAS_LOCAL).all())
    if not filas:
        return 0

    reales = {
        u.username: u
        for u in User.objects.using("default").filter(username__in=[f.username for f in filas])
    }
    revisadas = 0
    for fila in filas:
        real = reales.get(fila.username)
        if real is None:
            # El usuario ya no existe en Neon: no debe poder seguir
            # entrando sin conexión en esta máquina.
            fila.delete(using=ALIAS_LOCAL)
            logger.info("Credencial local eliminada: %s ya no existe en la base.", fila.username)
            revisadas += 1
            continue
        if real.password != fila.password_hash:
            fila.aviso_password_cambiada = True
            logger.warning("La contraseña de %s cambió en la base — se actualiza la caché local.", fila.username)
        fila.user_id = real.pk
        fila.password_hash = real.password
        fila.is_active = real.is_active
        fila.is_staff = real.is_staff
        fila.is_superuser = real.is_superuser
        fila.rol = rol_de(real)
        fila.save(using=ALIAS_LOCAL)
        revisadas += 1
    return revisadas


# --- Historial offline (prompt 19c, punto 1) ---------------------------------

LIMITE_HISTORIAL_CACHE = 300


def _fila_historial_a_payload(fila):
    """Convierte una fila de movimientos_periodo() (services.py) en algo JSON-serializable para MovimientoHistorialCache.payload."""
    return {
        "fecha": _serializar_valor(fila["fecha"]),
        "tipo": fila["tipo"],
        "producto_nombre": str(fila["producto"]),
        "cantidad": fila["cantidad"],
        "valor_unitario": _serializar_valor(fila["valor_unitario"]) if fila["valor_unitario"] is not None else None,
        "usuario": fila["usuario"],
        "detalle": fila["detalle"],
        "creado_en": _serializar_valor(fila["creado_en"]),
        "tipo_registro": fila["tipo_registro"],
        "registro_id": fila["registro_id"],
    }


def _payload_a_fila_historial(payload):
    """Inverso de _fila_historial_a_payload(): reconstruye la fila lista para la plantilla historial.html."""
    valor_unitario = payload.get("valor_unitario")
    return {
        "fecha": _deserializar_valor(payload["fecha"]),
        "tipo": payload["tipo"],
        "producto": payload["producto_nombre"],
        "cantidad": payload["cantidad"],
        "valor_unitario": _deserializar_valor(valor_unitario) if valor_unitario is not None else None,
        "usuario": payload.get("usuario") or "",
        "detalle": payload.get("detalle") or "",
        "creado_en": _deserializar_valor(payload["creado_en"]),
        "tipo_registro": payload["tipo_registro"],
        "registro_id": payload["registro_id"],
        "es_pendiente": False,
    }


def refrescar_historial_cache():
    """
    Copia local (borra y reinserta, últimos LIMITE_HISTORIAL_CACHE) de
    los movimientos ya confirmados en Neon — lo que permite consultar
    Historial sin conexión (prompt 19c, punto 1). Reutiliza
    movimientos_periodo() (services.py), la misma función que ya arma el
    Historial en línea, para no duplicar esa lógica.

    Solo tiene sentido llamarla con conexión — deja que la excepción se
    propague si no la hay, igual que refrescar_catalogo_cache().
    """
    preparar_bases_locales()
    from datetime import date as date_cls

    from .models import MovimientoHistorialCache
    from .services import movimientos_periodo

    filas = movimientos_periodo(date_cls(1900, 1, 1), date_cls(2999, 12, 31), descendente=True)
    filas = filas[:LIMITE_HISTORIAL_CACHE]

    with transaction.atomic(using=ALIAS_LOCAL):
        MovimientoHistorialCache.objects.using(ALIAS_LOCAL).all().delete()
        for fila in filas:
            MovimientoHistorialCache.objects.using(ALIAS_LOCAL).create(
                tipo_registro=fila["tipo_registro"],
                registro_id=fila["registro_id"],
                fecha=fila["fecha"],
                producto_id=fila["producto"].pk,
                payload=_fila_historial_a_payload(fila),
            )


def _fila_desde_pendiente_historial(pendiente, productos_por_id, usuarios_por_id):
    """
    Convierte un PendienteSincronizacion en una fila de historial —
    mismo formato que las de la caché, marcada aparte como es_pendiente.
    """
    payload = pendiente.payload
    producto_id = payload.get("producto_id")
    producto_nombre = productos_por_id.get(producto_id)
    if producto_nombre is None:
        return None

    if pendiente.modelo == "LoteCompra":
        cantidad = payload.get("cantidad") or 0
        valor_unitario = payload.get("costo_unitario")
        detalle = payload.get("notas") or ""
        if payload.get("proveedor"):
            detalle = f"Proveedor: {payload['proveedor']}" + (f" · {detalle}" if detalle else "")
    elif pendiente.modelo == "MovimientoSalida":
        cantidad = abs(payload.get("cantidad") or 0)
        valor_unitario = payload.get("precio_venta_unitario") if payload.get("tipo") == "venta" else payload.get("costo_unitario_snapshot")
        detalle = payload.get("motivo") or ""
    else:  # ConteoFisico
        cantidad = payload.get("cantidad_contada") or 0
        valor_unitario = None
        detalle = payload.get("notas") or ""

    return {
        "fecha": _deserializar_valor(payload.get("fecha")),
        "tipo": _tipo_mostrado_pendiente(pendiente),
        "producto": producto_nombre,
        "producto_id": producto_id,
        "cantidad": cantidad,
        "valor_unitario": _deserializar_valor(valor_unitario) if valor_unitario is not None else None,
        "usuario": usuarios_por_id.get(payload.get("registrado_por_id"), ""),
        "detalle": detalle,
        "creado_en": pendiente.creado_en,
        "tipo_registro": pendiente.modelo,
        "registro_id": None,  # todavía no existe en Neon — no se puede editar/eliminar hasta que sincronice
        "es_pendiente": True,
    }


def historial_offline(fecha_desde, fecha_hasta, producto_id=None):
    """
    Historial combinado sin conexión (prompt 19c, punto 1): los últimos
    movimientos ya sincronizados (MovimientoHistorialCache, refrescada
    con conexión cada INTERVALO_CATALOGO_SEGUNDOS) más lo que está en la
    cola de pendientes de ESTA máquina y todavía no ha llegado a Neon —
    marcado aparte con "es_pendiente" para que la plantilla lo distinga
    visualmente. Nunca incluye edición/eliminación (eso lo sigue
    bloqueando RequiereConexionMixin en CorreccionUpdateView/DeleteView,
    sin cambios de este prompt).

    Alcance explícito y limitado a propósito: son los últimos
    LIMITE_HISTORIAL_CACHE movimientos, no el historial completo — un
    filtro de fechas más viejo que lo cacheado simplemente no encuentra
    nada ahí, igual que pasaría si de verdad no hubiera movimientos en
    ese rango.
    """
    from .models import CredencialOfflineCache, MovimientoHistorialCache, PendienteSincronizacion, Producto

    filas = []
    cache_qs = MovimientoHistorialCache.objects.using(ALIAS_LOCAL).filter(fecha__gte=fecha_desde, fecha__lte=fecha_hasta)
    if producto_id:
        cache_qs = cache_qs.filter(producto_id=producto_id)
    for fila_cache in cache_qs:
        filas.append(_payload_a_fila_historial(fila_cache.payload))

    try:
        pendientes = list(PendienteSincronizacion.objects.using(ALIAS_LOCAL).all())
    except Exception:
        pendientes = []
    if pendientes:
        productos_por_id = {p.pk: p.nombre for p in Producto.objects.using(ALIAS_LOCAL).all()}
        usuarios_por_id = {c.user_id: c.username for c in CredencialOfflineCache.objects.using(ALIAS_LOCAL).all()}
        for pendiente in pendientes:
            fila = _fila_desde_pendiente_historial(pendiente, productos_por_id, usuarios_por_id)
            if fila is None:
                continue
            if producto_id and fila["producto_id"] != producto_id:
                continue
            if not (fecha_desde <= fila["fecha"] <= fecha_hasta):
                continue
            filas.append(fila)

    filas.sort(key=lambda f: (f["fecha"], f["creado_en"]), reverse=True)
    return filas


# --- Hilo de sincronización en segundo plano --------------------------------

INTERVALO_SEGUNDOS = 20
# El catálogo casi nunca cambia: refrescarlo cada 20 segundos era pagar
# una copia completa contra Neon todo el día para nada (y arriesgar que
# un fallo ahí tapara la sincronización, que es lo que de verdad urge).
INTERVALO_CATALOGO_SEGUNDOS = 300
# Las credenciales se revalidan mucho más seguido aunque cambien todavía
# menos: son 3 usuarios, una sola consulta barata, y de esa revalidación
# depende que una sesión iniciada sin conexión NO se cierre de golpe si
# la contraseña cambió en la nube (prompt 19b, punto 1). Un minuto acota
# esa ventana aunque el corte de red haya sido tan corto que el ciclo ni
# alcanzó a verlo.
INTERVALO_CREDENCIALES_SEGUNDOS = 60

_hilo_iniciado = False
_lock_hilo = threading.Lock()


def _tarea(nombre, funcion):
    """
    Corre una tarea del ciclo aislada de las demás: que una falle NUNCA
    debe impedir que las otras corran. Antes el ciclo entero iba en un
    solo try, así que un error refrescando el catálogo (que va primero)
    se llevaba consigo la sincronización de pendientes, que es lo único
    urgente.

    Devuelve True si la tarea se completó, False si hubo un problema de
    conexión (señal para que el ciclo se detenga: se volvió a caer la red).
    """
    try:
        funcion()
        return True
    except ERRORES_DE_CONEXION:
        logger.warning("Se cayó la conexión durante la tarea '%s' — se reintenta en el próximo ciclo.", nombre)
        return False
    except Exception:
        logger.exception("Error en la tarea '%s' del ciclo de sincronización.", nombre)
        return True


def _ciclo_de_fondo():
    preparar_bases_locales()
    ultimo_catalogo = 0.0
    ultimo_credenciales = 0.0
    habia_conexion = True
    while True:
        try:
            # close_old_connections() en CADA vuelta: este hilo no es una
            # request, así que Django nunca corre por su cuenta el
            # reciclado de conexiones que sí protege a las requests
            # normales. Sin esto, la primera caída de red dejaba la
            # conexión de este hilo rota PARA SIEMPRE (ver el bug raíz
            # descrito en _reciclar_conexion_remota).
            close_old_connections()

            conectado = hay_conexion()
            if conectado:
                sincronizados = 0

                def _sincronizar():
                    nonlocal sincronizados
                    sincronizados = sincronizar_pendientes()

                if _tarea("sincronizar pendientes", _sincronizar) and sincronizados:
                    logger.info("Sincronización offline: %d movimiento(s) confirmados contra Neon.", sincronizados)
                    # Refresca el historial cacheado de inmediato tras
                    # sincronizar de verdad algo (prompt 19c, punto 1/2):
                    # así Historial offline y el próximo reintento del
                    # dashboard ya reflejan lo recién subido, sin esperar
                    # hasta el próximo refresco de INTERVALO_CATALOGO_SEGUNDOS.
                    _tarea("refrescar historial tras sincronizar", refrescar_historial_cache)

                # "not habia_conexion" = acaba de VOLVER la conexión: se
                # refresca de inmediato, sin esperar el intervalo. Importa
                # sobre todo para la revalidación de credenciales (prompt
                # 19b, punto 1) — hasta que corre, una sesión iniciada sin
                # conexión cuya contraseña cambió en la nube se cerraría
                # sola en la siguiente request, que es justo lo que el
                # aviso está para evitar.
                ahora = time.monotonic()
                acaba_de_reconectar = not habia_conexion
                if acaba_de_reconectar or ahora - ultimo_credenciales >= INTERVALO_CREDENCIALES_SEGUNDOS:
                    if _tarea("revalidar credenciales", refrescar_credenciales_cache):
                        ultimo_credenciales = ahora
                if acaba_de_reconectar or ahora - ultimo_catalogo >= INTERVALO_CATALOGO_SEGUNDOS:
                    ok_catalogo = _tarea("refrescar catálogo", refrescar_catalogo_cache)
                    ok_historial = _tarea("refrescar historial", refrescar_historial_cache)
                    if ok_catalogo and ok_historial:
                        ultimo_catalogo = ahora
            habia_conexion = conectado
        except Exception:
            # Nunca debe matar el hilo — se reintenta solo en el próximo ciclo.
            logger.exception("Error en el ciclo de sincronización offline.")
        time.sleep(INTERVALO_SEGUNDOS)


def iniciar_hilo_sincronizacion():
    """
    Arranca el hilo daemon que sube la cola de pendientes y detecta solo
    que la conexión volvió. Idempotente: se puede llamar más de una vez
    en el mismo proceso (lo hacen tanto InventarioConfig.ready() como
    app_desktop.py) y solo arranca un hilo.

    No prepara la base local aquí: eso lo hace el propio hilo en su
    primera vuelta, para que arrancar la app nunca dependa de una
    migración corriendo en el hilo principal.
    """
    global _hilo_iniciado
    with _lock_hilo:
        if _hilo_iniciado:
            return
        hilo = threading.Thread(target=_ciclo_de_fondo, name="sincronizacion-offline", daemon=True)
        hilo.start()
        _hilo_iniciado = True


# Comandos de manage.py que NO deben arrancar el hilo: o no sirven a
# nadie (una migración, un chequeo), o meterían escrituras reales en
# medio de una prueba.
_COMANDOS_SIN_HILO = {
    "test", "migrate", "makemigrations", "showmigrations", "sqlmigrate", "check",
    "shell", "dbshell", "collectstatic", "createsuperuser", "changepassword",
    "dumpdata", "loaddata", "flush", "crear_grupos", "respaldo_completo",
}


def iniciar_hilo_sincronizacion_si_corresponde():
    """
    Punto de arranque automático, desde InventarioConfig.ready().

    Antes el hilo SOLO se arrancaba desde app_desktop.py, así que
    levantar el proyecto con `manage.py runserver` (justo como se prueba
    en desarrollo) no sincronizaba absolutamente nada — otra razón por la
    que el prompt 19b, punto 2, se veía como "nunca sincroniza".
    """
    import os

    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando in _COMANDOS_SIN_HILO:
        return
    if comando == "runserver" and os.environ.get("RUN_MAIN") != "true":
        # Proceso padre del autoreloader: solo vigila archivos, no atiende
        # requests — un segundo hilo ahí solo duplicaría el trabajo.
        return
    iniciar_hilo_sincronizacion()

# inventario/resiliencia.py
"""
Manejo de errores de conexión a la base de datos (prompt 18b, punto 2;
ampliado a manejo GLOBAL en el prompt 19b, punto 4).

Tres mecanismos, para tres situaciones distintas:

- Escrituras (LoteCompra, MovimientoSalida, ConteoFisico): reintentar
  tiene sentido — un corte de red momentáneo a media escritura no
  debería perder el registro que el usuario acaba de llenar, y
  reintentar 2-3 veces con una espera corta no tiene efectos secundarios
  (si el primer intento no llegó a confirmarse en la base de datos, no
  hay nada que duplicar). Ver ReintentoEscrituraMixin.
- Vistas que NO pueden funcionar sin conexión (reportes, historial,
  catálogo, correcciones): se bloquean por adelantado con un mensaje
  claro, sin dejar que revienten a media consulta. Ver
  RequiereConexionMixin / requiere_conexion.
- Cualquier otra cosa: la red de seguridad global.
  ManejoGlobalErrorConexionMiddleware atrapa RequiereConexionError y
  cualquier OperationalError/InterfaceError que se escape de CUALQUIER
  vista —incluida /admin/ de Django y cualquier vista futura que nadie
  se acuerde de clasificar— y muestra la pantalla amigable en vez de la
  página técnica de Django.

  Esto es lo que corrige el bug del prompt 19b, punto 4: el manejo del
  prompt 18b se aplicaba vista por vista (un mixin puesto a mano en
  unas pocas), así que TODA vista no cubierta —y las nuevas del motor
  offline— seguía reventando sin manejar al navegar sin conexión.

CLASIFICACIÓN DE VISTAS SIN CONEXIÓN (prompt 19b, punto 4)
==========================================================
Toda vista de la app está clasificada explícitamente en uno de los dos
grupos, y hay una prueba automatizada (ClasificacionOfflineTests en
tests.py) que falla si alguna queda sin clasificar:

FUNCIONAN SIN CONEXIÓN — marcadas con `funciona_sin_conexion = True`
  home ................................ dashboard básico con catálogo cacheado
  movimientosalida_create ............. venta/merma/ajuste -> cola local
  lotecompra_create ................... entrada -> cola local
  conteofisico_create ................. conteo físico -> cola local
  instrucciones ....................... contenido estático, no toca la base
  login / logout ...................... caché local de credenciales
  historial ............................ lectura combinada: caché de los últimos
                                          movimientos + cola de pendientes de esta
                                          máquina (prompt 19c, punto 1) — NUNCA
                                          incluye editar/eliminar, eso lo bloquean
                                          las vistas de corrección de abajo
  cola_sincronizacion .................. lee la cola local (prompt 19c, punto 4)
  cola_sincronizacion_reintentar(_todos) intenta sincronizar; si no hay conexión,
                                          lo reporta con un mensaje, no revienta
  estado_sincronizacion ................ JSON liviano para el auto-refresco del
                                          dashboard (prompt 19c, punto 2)

REQUIEREN CONEXIÓN — marcadas con `requiere_conexion_activa = True`
  categoria_list / _create / _update / _toggle .... catálogo maestro
  producto_list / _create / _update / _toggle ..... catálogo maestro
  conteofisico_detail ............................. necesita el stock teórico real
  conteofisico_generar_ajuste ..................... escribe con transacción y bloqueo
  reportes ........................................ lectura pesada contra Neon
  correcciones_historial .......................... bitácora de correcciones
  *_correccion_editar / *_correccion_eliminar ..... editar/eliminar historial: NUNCA
                                                    se encola, ni bajo ninguna
                                                    circunstancia (prompt 17/19) —
                                                    ni siquiera ahora que Historial
                                                    en sí ya no requiere conexión
  /admin/ de Django ............................... cubierto por el middleware global
"""
import time
from functools import wraps

from django.db import InterfaceError, OperationalError
from django.shortcuts import render

from .offline import hay_conexion

# Errores de conexión propiamente (el servidor se volvió inalcanzable, la
# conexión se cortó, el socket se cerró) — nunca IntegrityError (una
# restricción real violada) ni ValidationError (datos inválidos): esos no
# se arreglan reintentando ni tienen nada que ver con la red.
ERRORES_DE_CONEXION = (OperationalError, InterfaceError)


class RequiereConexionError(Exception):
    """
    "Esta función necesita internet y ahora mismo no hay."

    A diferencia de un OperationalError (que aparece cuando algo ya
    reventó a media consulta), esta se lanza A PROPÓSITO y por
    adelantado, antes de intentar nada — así el usuario ve el mensaje
    claro de inmediato en vez de esperar a que una consulta pesada
    agote su tiempo de espera.
    """


def _pantalla_sin_conexion(request, requiere_conexion):
    return render(
        request,
        "inventario/error_conexion.html",
        {"requiere_conexion": requiere_conexion},
        status=503,
    )


class ManejoGlobalErrorConexionMiddleware:
    """
    Red de seguridad global: ninguna vista de la app puede terminar en la
    página técnica de error de Django por un problema de conexión.

    process_exception() es el punto correcto para esto porque Django lo
    llama tanto para las excepciones de la vista como para las que se
    disparan al renderizar la plantilla de un TemplateResponse (que es lo
    que devuelven las vistas genéricas) — un error de conexión dentro de
    una plantilla también queda cubierto.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, RequiereConexionError):
            return _pantalla_sin_conexion(request, requiere_conexion=True)
        if isinstance(exception, ERRORES_DE_CONEXION):
            return _pantalla_sin_conexion(request, requiere_conexion=not hay_conexion())
        return None


class RequiereConexionMixin:
    """
    Para vistas que NO pueden funcionar sin conexión: corta de entrada
    con el mensaje claro. `requiere_conexion_activa` es además la marca
    que lee la prueba de clasificación (ver el docstring del módulo).
    """
    requiere_conexion_activa = True

    def dispatch(self, request, *args, **kwargs):
        if not hay_conexion():
            raise RequiereConexionError(self.__class__.__name__)
        return super().dispatch(request, *args, **kwargs)


def requiere_conexion(view_func):
    """Versión decorador de RequiereConexionMixin, para vistas basadas en función."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not hay_conexion():
            raise RequiereConexionError(getattr(view_func, "__name__", "vista"))
        return view_func(request, *args, **kwargs)

    wrapper.requiere_conexion_activa = True
    return wrapper


def funciona_sin_conexion(view_func):
    """
    Marca una vista basada en función como parte del grupo que SÍ debe
    funcionar sin conexión. No cambia el comportamiento — es la
    contraparte declarativa de requiere_conexion, para que la prueba de
    clasificación pueda exigir que ninguna vista quede sin clasificar.
    """
    view_func.funciona_sin_conexion = True
    return view_func


class ReintentoEscrituraMixin:
    """
    Reintenta el guardado real (form.save(), vía form_valid()) hasta
    `intentos_maximos` veces si falla por un error de conexión.

    Debe ir DESPUÉS de ProteccionDobleSubmitMixin en la lista de clases
    base de la vista (ver antiduplicado.py): así el token de un solo uso
    se consume una sola vez, antes de entrar al bucle de reintento — un
    reintento de la escritura nunca se confunde con un doble-submit ni
    vuelve a pasar por esa validación.

    Ojo: ColaOfflineMixin va por ENCIMA de este mixin y, cuando ya sabe
    que no hay conexión, ni siquiera lo invoca — no tiene sentido hacer
    esperar al usuario 3 intentos por algo que ya se sabe que va a
    fallar (ver ColaOfflineMixin en inventario/offline.py).
    """
    intentos_maximos = 3
    espera_entre_intentos_segundos = 0.5

    def form_valid(self, form):
        ultimo_error = None
        for intento in range(1, self.intentos_maximos + 1):
            try:
                return super().form_valid(form)
            except ERRORES_DE_CONEXION as error:
                ultimo_error = error
                if intento < self.intentos_maximos:
                    time.sleep(self.espera_entre_intentos_segundos)
        raise ultimo_error


class ManejoErrorConexionMixin:
    """
    Muestra la pantalla amigable si la conexión falla a medio camino.

    Desde el prompt 19b esto lo cubre también el middleware global, así
    que este mixin ya no es la única protección de nadie — se conserva
    para las vistas que quieran manejarlo dentro de su propio dispatch
    (por ejemplo, para no depender del orden de los middlewares).
    """

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except ERRORES_DE_CONEXION:
            return _pantalla_sin_conexion(request, requiere_conexion=not hay_conexion())


def manejar_error_conexion(view_func):
    """Versión decorador de ManejoErrorConexionMixin, para vistas basadas en función (ej. home())."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except ERRORES_DE_CONEXION:
            return _pantalla_sin_conexion(request, requiere_conexion=not hay_conexion())
    return wrapper

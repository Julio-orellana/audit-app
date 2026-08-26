# inventario/resiliencia.py
"""
Manejo de errores de conexión a la base de datos (prompt 18b, punto 2) —
más allá de CONN_HEALTH_CHECKS (prompt 18), que solo reconecta una
conexión que ya estaba muerta ANTES de que empezara la request. Nada
protegía hasta ahora una conexión que se cae A MEDIO de una operación ya
en curso — eso reventaba como un 500 sin manejar, con la página técnica
de error de Django encima (DEBUG=True, ver prompt 22).

Dos mecanismos, para dos situaciones distintas:

- Escrituras (LoteCompra, MovimientoSalida, ConteoFisico): reintentar
  tiene sentido — un corte de red momentáneo a media escritura no
  debería perder el registro que el usuario acaba de llenar, y
  reintentar 2-3 veces con una espera corta no tiene efectos secundarios
  (si el primer intento no llegó a confirmarse en la base de datos, no
  hay nada que duplicar). Esto además es la base que va a necesitar el
  motor de sincronización offline del prompt 19, que de todas formas
  necesita lógica de reintento.
- Lecturas pesadas (Dashboard, Reportes, Historial): reintentar una
  consulta de cientos de filas no vale la complejidad para un caso poco
  frecuente — en vez de eso, se atrapa el error y se muestra un mensaje
  claro pidiendo recargar, en lugar de dejar que se propague la página
  técnica de Django.
"""
import time
from functools import wraps

from django.db import InterfaceError, OperationalError
from django.shortcuts import render

# Errores de conexión propiamente (el servidor se volvió inalcanzable, la
# conexión se cortó, el socket se cerró) — nunca IntegrityError (una
# restricción real violada) ni ValidationError (datos inválidos): esos no
# se arreglan reintentando ni tienen nada que ver con la red.
ERRORES_DE_CONEXION = (OperationalError, InterfaceError)


class ReintentoEscrituraMixin:
    """
    Reintenta el guardado real (form.save(), vía form_valid()) hasta
    `intentos_maximos` veces si falla por un error de conexión.

    Debe ir DESPUÉS de ProteccionDobleSubmitMixin en la lista de clases
    base de la vista (ver antiduplicado.py): así el token de un solo uso
    se consume una sola vez, antes de entrar al bucle de reintento — un
    reintento de la escritura nunca se confunde con un doble-submit ni
    vuelve a pasar por esa validación.
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
    Para vistas de lectura pesada (Historial, Reportes): si la conexión a
    la base de datos falla a medio camino, muestra una página clara en
    vez de la técnica de Django. No reintenta — ver docstring del módulo.
    """

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except ERRORES_DE_CONEXION:
            return render(request, "inventario/error_conexion.html", status=503)


def manejar_error_conexion(view_func):
    """Versión decorador de ManejoErrorConexionMixin, para vistas basadas en función (ej. home())."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except ERRORES_DE_CONEXION:
            return render(request, "inventario/error_conexion.html", status=503)
    return wrapper

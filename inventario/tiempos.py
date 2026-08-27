# inventario/tiempos.py
"""
Instrumentación de tiempo real de extremo a extremo por request (prompt 24).

El benchmark del prompt 18b medía con django.test.Client — nunca abre una
conexión TCP/TLS real ni pasa por el WSGI server real (waitress), así que
no capturaba el costo fijo por request que sí se siente navegando en un
navegador real. Este middleware mide el tiempo real, desde que la request
entra hasta que la respuesta sale, tal como lo mediría cualquier
herramienta externa (curl -w, las devtools del navegador) — el número que
hay que comparar contra lo que se "siente" al navegar.
"""
import logging
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger("inventario.tiempos")


class TiempoRequestMiddleware:
    """
    Debe ir PRIMERO en MIDDLEWARE (envuelve todo lo demás — Security,
    Session, Csrf, Auth, etc. — para medir el costo real de la request
    completa, no solo el de la vista). Loguea un renglón por request a
    nivel INFO; ver LOGGING en settings.py para dónde termina ese log.

    Con DEBUG=True, Django ya registra cada consulta ejecutada en
    connection.queries (con su tiempo individual) — se aprovecha ese
    registro, ya existente, para separar "tiempo total de la request" de
    "tiempo total esperando a la base de datos" sin agregar overhead
    propio de instrumentación.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        inicio = time.perf_counter()
        if settings.DEBUG:
            connection.queries_log.clear()
        response = self.get_response(request)
        duracion_ms = (time.perf_counter() - inicio) * 1000
        if settings.DEBUG:
            n_queries = len(connection.queries)
            tiempo_bd_ms = sum(float(q["time"]) for q in connection.queries) * 1000
            logger.info(
                "TIEMPO %5.0fms  (bd=%5.0fms en %2d consultas)  %-4s %3d  %s",
                duracion_ms,
                tiempo_bd_ms,
                n_queries,
                request.method,
                response.status_code,
                request.get_full_path(),
            )
        else:
            logger.info(
                "TIEMPO %5.0fms  %-4s %3d  %s",
                duracion_ms,
                request.method,
                response.status_code,
                request.get_full_path(),
            )
        return response

# inventario/db_backend/base.py
"""
Backend de PostgreSQL que NO reintenta la red cuando ya consta que la
nube está caída (prompt 33c).

Por qué existe
--------------
La caché de resultado negativo vivía solo dentro de hay_conexion(), así
que únicamente ahorraba tiempo a quien preguntaba por ahí. Cualquier
consulta directa al ORM —una vista que hace Producto.objects.filter(),
el ORM resolviendo un ForeignKey, una comprobación de permisos— abría su
propio intento de conexión y pagaba el timeout entero, sin enterarse de
que 200 milisegundos antes otro hilo ya había comprobado que no hay red.

Medido en la VM de Windows, sin conexión y con connect_timeout=3:

    28506ms  GET 503  /correcciones/
    28477ms  GET 200  /sincronizacion/
    27660ms  GET 503  /salidas/176/editar/
    19450ms  POST 302 /login/
     9311ms  GET 200  /historial/

9.2s es UN intento (connect_timeout es por dirección IP y el host de
Neon resuelve a 3, así que 3 x 3s). 18.4s son dos y 27.6s son tres: hay
caminos que intentan conectarse hasta tres veces dentro de una sola
request. Ninguna cantidad de arreglos vista por vista cubre eso, porque
el intento nace abajo, en la capa de conexión.

Qué hace
--------
Se pone el corto donde de verdad nace el intento: si ya consta que no
hay conexión, get_new_connection() falla al instante con el mismo
OperationalError que produciría la red, en vez de esperar el timeout.
Para todo lo de arriba —vistas, middleware, motor offline— es
indistinguible de un corte normal, así que no hay que tocar nada más y
los caminos futuros quedan cubiertos solos.

No se puede quedar pegado en "sin conexión": hay_conexion() consulta la
caché ANTES de intentar, así que solo llega aquí cuando la caché ya
venció, y en ese momento el corto no aplica y el sondeo sale de verdad a
la red. Así se sigue reintentando cada SEGUNDOS_CACHE_SIN_CONEXION.
"""
from django.db.backends.postgresql import base


class DatabaseWrapper(base.DatabaseWrapper):
    def get_new_connection(self, conn_params):
        # Import perezoso a propósito: este módulo lo carga Django al
        # construir las conexiones, antes de que las apps estén listas,
        # e inventario.offline importa modelos.
        from django.conf import settings

        from ..offline import esperar_sondeo_en_curso, sin_conexion_reciente

        if getattr(settings, "BD_NUBE_NO_CONFIGURADA", False):
            # Sin configuración legible el host es un centinela
            # inalcanzable a propósito: no tiene sentido resolverlo ni
            # una vez. Este caso no vence nunca —no se arregla solo—,
            # así que aquí sí se corta siempre.
            raise self.Database.OperationalError(
                "No se pudo leer la configuración de la base de datos en la nube. La app "
                "opera sobre la cola y el caché local; esto no se arregla reconectando a "
                "internet, hace falta corregir el archivo .env junto al ejecutable."
            )

        if sin_conexion_reciente():
            # HECHO: un sondeo real falló hace pocos segundos. No es una
            # conjetura, así que cortar aquí no puede producir un falso
            # negativo (prompt 33d).
            raise self.Database.OperationalError(
                "Sin conexión con la base en la nube (un sondeo real falló hace unos "
                "segundos). No se reintenta todavía para no dejar la ventana esperando; el "
                "motor offline vuelve a probar solo en cuanto expire ese margen."
            )

        # Si hay un sondeo EN CURSO, se espera su resultado real en vez de
        # abrir una segunda conexión en paralelo. Esto es lo que antes
        # hacía la conjetura de los 2 segundos —"lleva rato, doy por hecho
        # que no hay red"— y era la causa del prompt 33d: un sondeo sano
        # pero lento bastaba para que este corto rechazara una conexión
        # perfectamente posible, y el login terminara resolviéndose contra
        # la caché local con internet impecable. Esperar el resultado
        # verdadero conserva la protección contra la avalancha sin poder
        # equivocarse.
        if esperar_sondeo_en_curso() is False:
            raise self.Database.OperationalError(
                "Sin conexión con la base en la nube (se esperó el resultado del sondeo "
                "que estaba en curso y falló)."
            )
        return super().get_new_connection(conn_params)

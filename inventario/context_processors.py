# inventario/context_processors.py
"""Expone el rol del usuario a todas las plantillas, para que el navbar
(y cualquier otra plantilla) pueda adaptarse por rol sin que cada vista
tenga que pasarlo a mano en su contexto."""
from .offline import contar_pendientes
from .permisos import rol_de


def rol(request):
    if request.user.is_authenticated:
        return {"rol_usuario": rol_de(request.user)}
    return {"rol_usuario": None}


def pendientes_sincronizar(request):
    """
    Cantidad de movimientos pendientes de sincronizar (prompt 19), visible
    en el navbar de CUALQUIER página — no solo el dashboard — para que
    Mich2026/Ruth confíen en que nada se perdió, y para que el vendedor
    entienda que no debe apagar el equipo mientras haya pendientes. Lee
    la cola LOCAL (SQLite), nunca Neon — barato, funciona sin conexión.

    Desde el prompt 19b la cola es la misma para los tres roles (un solo
    archivo local), así que ya no depende del rol de quien pregunta.
    """
    if not request.user.is_authenticated:
        return {"pendientes_navbar": 0}
    return {"pendientes_navbar": contar_pendientes()}


def configuracion_bd(request):
    """
    Avisa a la plantilla si la app está corriendo SIN configuración de
    base de datos en la nube (prompt 33): falta el archivo .env junto al
    ejecutable, o su DATABASE_URL es inválida.

    Es un estado DISTINTO de "no hay internet", y por eso se expone
    aparte: un corte de red se arregla solo al volver la conexión, esto
    no — hace falta que alguien coloque el archivo de configuración. Sin
    diferenciarlo, Ruth/Michelle verían el aviso normal de "sin
    conexión" y perderían tiempo esperando a que se resuelva solo.

    La app sigue funcionando con normalidad sobre la cola y el caché
    local, igual que ante un corte de red — misma filosofía que el resto
    del motor offline: perder la base es un estado esperado, nunca un
    motivo para negarle la entrada a nadie.
    """
    from django.conf import settings

    return {"bd_nube_no_configurada": getattr(settings, "BD_NUBE_NO_CONFIGURADA", False)}

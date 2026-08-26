# inventario/context_processors.py
"""Expone el rol del usuario a todas las plantillas, para que el navbar
(y cualquier otra plantilla) pueda adaptarse por rol sin que cada vista
tenga que pasarlo a mano en su contexto."""
from .permisos import rol_de


def rol(request):
    if request.user.is_authenticated:
        return {"rol_usuario": rol_de(request.user)}
    return {"rol_usuario": None}

# inventario/permisos.py
"""
Control de acceso por rol (prompt 16): tres Group de Django — "admin",
"auditor", "vendedor" — asignados a cada usuario (ver el management
command crear_grupos). RequiereRol()/requiere_rol() son la única fuente de
verdad para "qué rol(es) puede ver esta vista", tanto para vistas basadas
en clase como en función — el bloqueo ocurre en la vista misma (403 si el
usuario está autenticado pero no tiene el rol, redirección a login si no
lo está), nunca solo ocultando un enlace en el template.

Cuando se implemente editar/eliminar historial (prompt 17), esas vistas
deben usar RequiereRol("admin") — es exclusivo de administrador, ni
siquiera el auditor puede editar o borrar lo ya registrado.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied


def _tiene_rol(user, roles):
    return user.is_authenticated and user.groups.filter(name__in=roles).exists()


def rol_de(user):
    """
    Rol principal del usuario, para vistas cuyo contenido varía por rol en
    vez de simplemente permitirse o no (ej. home()). Se resuelve en orden
    admin > auditor > vendedor; alguien sin ninguno de los tres grupos se
    trata como vendedor — el más restringido — para nunca mostrar de más
    por accidente ante un usuario sin rol asignado.
    """
    if user.groups.filter(name="admin").exists():
        return "admin"
    if user.groups.filter(name="auditor").exists():
        return "auditor"
    return "vendedor"


def RequiereRol(*roles):
    """
    Mixin para vistas basadas en clase: RequiereRol("admin", "auditor")
    como clase base. Sin sesión iniciada -> redirige a login; con sesión
    pero sin el rol -> 403 (nunca deja pasar solo porque el enlace esté
    oculto en el template).
    """
    class _RequiereRolMixin(LoginRequiredMixin, UserPassesTestMixin):
        raise_exception = True

        def test_func(self):
            return _tiene_rol(self.request.user, roles)

    return _RequiereRolMixin


def requiere_rol(*roles):
    """Decorador equivalente a RequiereRol(), para vistas basadas en función."""
    def decorador(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not _tiene_rol(request.user, roles):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return login_required(wrapper)
    return decorador

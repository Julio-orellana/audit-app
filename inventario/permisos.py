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
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def _tiene_rol(user, roles):
    if not user.is_authenticated:
        return False
    if hasattr(user, "_rol_cache"):
        # Prompt 19: cuando el usuario viene resuelto desde la caché
        # offline (sin conexión — ver BackendConRespaldoOffline.get_user()
        # en inventario/offline.py), no existe una consulta de grupos real
        # que hacer — _rol_cache YA es la única fuente de verdad posible
        # en ese caso. Usarla también aquí (antes esta función siempre
        # consultaba .groups directo, sin pasar por rol_de()) es lo que
        # hace posible que RequiereRol/requiere_rol sigan funcionando sin
        # conexión — de otro modo la cola offline nunca llegaría a
        # aplicarse: la request moriría aquí, antes que en la vista.
        return user._rol_cache in roles
    return user.groups.filter(name__in=roles).exists()


def rol_de(user):
    """
    Rol principal del usuario, para vistas cuyo contenido varía por rol en
    vez de simplemente permitirse o no (ej. home()). Se resuelve en orden
    admin > auditor > vendedor; alguien sin ninguno de los tres grupos se
    trata como vendedor — el más restringido — para nunca mostrar de más
    por accidente ante un usuario sin rol asignado.

    Se cachea en el propio objeto `user` (prompt 24): dentro de una misma
    request, rol_de() se llama más de una vez con el mismo usuario (la
    vista y, después, el context processor que expone "rol_usuario" a la
    plantilla) — sin esto, cada llamada repetía la(s) misma(s) consulta(s)
    de grupo, pagando otra vez la latencia de red fija contra Neon por
    algo que no cambia dentro de una request. Mismo patrón que ya usa el
    propio ModelBackend de Django para cachear permisos en el usuario
    (_perm_cache) — seguro porque AuthenticationMiddleware arma un
    usuario nuevo (y por lo tanto sin caché) en cada request.
    """
    if hasattr(user, "_rol_cache"):
        return user._rol_cache
    if user.groups.filter(name="admin").exists():
        rol = "admin"
    elif user.groups.filter(name="auditor").exists():
        rol = "auditor"
    else:
        rol = "vendedor"
    user._rol_cache = rol
    return rol


def RequiereRol(*roles):
    """
    Mixin para vistas basadas en clase: RequiereRol("admin", "auditor")
    como clase base. Sin sesión iniciada -> redirige a login; con sesión
    pero sin el rol -> 403 (nunca deja pasar solo porque el enlace esté
    oculto en el template).
    """
    class _RequiereRolMixin(LoginRequiredMixin, UserPassesTestMixin):
        def test_func(self):
            return _tiene_rol(self.request.user, roles)

        def handle_no_permission(self):
            """
            Distingue "no eres tú" de "no tienes permiso" (prompt 37).

            Antes esto era `raise_exception = True`, que es lo que la
            documentación de Django recomienda para forzar el 403 — pero
            UserPassesTestMixin lo aplica a los DOS casos, así que un
            visitante sin sesión que abriera /historial/ recibía un 403
            pelado en lugar del formulario de login. En el .exe eso es
            peor que un detalle: cuando la sesión caduca, la persona ve
            una página de error sin ninguna salida, en vez de que le
            pidan entrar otra vez.

            Las vistas de función nunca tuvieron el problema, porque
            requiere_rol() envuelve con login_required() y ese corre
            primero. Esto deja a las vistas de clase con el mismo
            comportamiento, que además es el que permisos.py ya decía
            tener escrito arriba.
            """
            if not self.request.user.is_authenticated:
                return redirect_to_login(
                    self.request.get_full_path(),
                    self.get_login_url(),
                    self.get_redirect_field_name(),
                )
            raise PermissionDenied

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

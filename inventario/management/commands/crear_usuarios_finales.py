# inventario/management/commands/crear_usuarios_finales.py
"""
Crea (o actualiza) los usuarios reales del sistema.

Las contraseñas NUNCA se escriben en este archivo: se leen de variables de
entorno al momento de ejecutar el comando, y Django las hashea de inmediato
con set_password() — nunca quedan en texto plano ni en este script ni en la
base de datos.

Uso:
    MICH_USERNAME=... MICH_PASSWORD=... \
    RUTH_USERNAME=... RUTH_PASSWORD=... \
    python manage.py crear_usuarios_finales

Nota sobre permisos (ver aviso en la respuesta del prompt 13a): el sistema
todavía no distingue permisos de "auditor" vs "admin" a nivel de vistas —
ambos usuarios pueden usar toda la app de inventario por igual hoy. La
única diferencia real que aplica este comando es is_staff/is_superuser,
que solo controla el acceso al admin de Django (/admin/), no a las
pantallas normales de la app.
"""
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

USUARIOS = [
    {
        "env_user": "MICH_USERNAME",
        "env_pass": "MICH_PASSWORD",
        "rol": "auditor operativo",
        "is_staff": False,
        "is_superuser": False,
    },
    {
        "env_user": "RUTH_USERNAME",
        "env_pass": "RUTH_PASSWORD",
        "rol": "administrador",
        "is_staff": True,
        "is_superuser": True,
    },
]


class Command(BaseCommand):
    help = (
        "Crea los usuarios reales del sistema (auditor y administrador). "
        "Las contraseñas se leen de variables de entorno, nunca se "
        "hardcodean aquí."
    )

    def handle(self, *args, **options):
        for datos in USUARIOS:
            username = os.environ.get(datos["env_user"])
            password = os.environ.get(datos["env_pass"])
            if not username or not password:
                raise CommandError(
                    f"Faltan las variables de entorno {datos['env_user']} y/o "
                    f"{datos['env_pass']}. Este comando no trae contraseñas por "
                    f"defecto a propósito."
                )

            usuario, creado = User.objects.get_or_create(username=username)
            usuario.is_staff = datos["is_staff"]
            usuario.is_superuser = datos["is_superuser"]
            usuario.set_password(password)  # Django la hashea, nunca queda en texto plano
            usuario.save()

            accion = "creado" if creado else "actualizado (ya existía)"
            self.stdout.write(self.style.SUCCESS(f"Usuario {datos['rol']} '{username}' {accion}."))

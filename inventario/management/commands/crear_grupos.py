# inventario/management/commands/crear_grupos.py
"""
Crea los tres Group de rol (prompt 16) si no existen todavía —
"admin", "auditor", "vendedor" — y asigna a los usuarios reales ya
creados (prompt 13a) su rol correspondiente: Mich2026 -> auditor,
Ruth -> admin. No toca contraseñas ni ningún otro dato de esos usuarios,
solo su membresía de grupo. Idempotente.
"""
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

GRUPOS = ("admin", "auditor", "vendedor")

# (username, nombre del grupo)
ASIGNACIONES = [
    ("Mich2026", "auditor"),
    ("Ruth", "admin"),
]


class Command(BaseCommand):
    help = "Crea los grupos admin/auditor/vendedor y asigna Mich2026->auditor, Ruth->admin."

    def handle(self, *args, **options):
        grupos = {}
        for nombre in GRUPOS:
            grupo, creado = Group.objects.get_or_create(name=nombre)
            grupos[nombre] = grupo
            accion = "creado" if creado else "ya existía"
            self.stdout.write(self.style.SUCCESS(f"Grupo '{nombre}' {accion}."))

        for username, nombre_grupo in ASIGNACIONES:
            try:
                usuario = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.WARNING(
                    f"Usuario '{username}' no existe todavía — córrelo después de crear_usuarios_finales."
                ))
                continue
            usuario.groups.add(grupos[nombre_grupo])
            self.stdout.write(self.style.SUCCESS(f"Usuario '{username}' asignado al grupo '{nombre_grupo}'."))

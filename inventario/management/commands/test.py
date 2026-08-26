# inventario/management/commands/test.py
"""
Sobreescribe el comando `test` de Django para que use --keepdb por
defecto (prompt 18b, punto 3).

Corriendo contra Neon, `manage.py test` sin más falla al intentar
DROP DATABASE al final porque el pooler de Neon no siempre libera la
conexión lo bastante rápido ("database ... is being accessed by other
users") — ver diagnóstico del prompt 20 y el hallazgo del prompt 18.
Con --keepdb, Django crea la base de pruebas una sola vez (o la
reutiliza si ya existe, aplicando cualquier migración pendiente) y
nunca intenta borrarla al final — se evita el DROP por completo, sin
depender de mover el .env a un lado como workaround.

La base de pruebas ("test_<nombre de la base real>") es una base
DISTINTA de la real — Django siempre las separa así — nunca toca los
datos reales del catálogo/usuarios/movimientos.

Si alguna vez hace falta forzar una base de pruebas nueva desde cero
(ej. las migraciones cambiaron de forma incompatible con la que ya
existe), --keepdb es un flag de encendido/apagado sin opuesto en Django
(no existe un --no-keepdb) — lo más simple es borrar la base
"test_<nombre>" a mano desde la consola de Neon, o correr las pruebas
contra SQLite moviendo el .env a un lado (ver EMPAQUETADO.md).
"""
from django.core.management.commands.test import Command as ComandoTestDeDjango


class Command(ComandoTestDeDjango):
    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.set_defaults(keepdb=True)

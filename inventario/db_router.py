# inventario/db_router.py
"""
Router de bases de datos (prompt 19, motor de sincronización offline).

Dos alias en juego (ver DATABASES en settings.py):
- "default": Neon (Postgres) en uso normal, o SQLite de desarrollo si no
  hay DATABASE_URL — sin cambios de los prompts anteriores.
- "local_disco": SQLite en un archivo en BASE_DIR_ESCRIBIBLE — la copia
  local cacheada de Producto/Categoria (para que el dashboard básico y el
  formulario de venta funcionen sin conexión), las credenciales cacheadas
  (para poder iniciar sesión sin internet) y la cola de pendientes por
  sincronizar. Al ser un archivo, todo eso sobrevive a un reinicio o un
  apagón — para los TRES roles, incluido vendedor (prompt 19b, punto 3:
  antes existía un alias "local_memoria" en RAM solo para el vendedor,
  decisión que se revirtió explícitamente).

Este router SOLO decide qué modelos pueden vivir en cada alias durante
`migrate` (allow_migrate) — las consultas en sí siempre usan el alias que
pide explícitamente quien llama (`.using("local_disco")`, etc.), este
router no las redirige.

Por qué hace falta: sin esto, `migrate --database=local_disco` intentaría
crear TODAS las tablas de Django ahí (auth, sessions, admin,
contenttypes, y también LoteCompra/MovimientoSalida/ConteoFisico/
CorreccionHistorial, que representan hechos ya confirmados y NUNCA deben
vivir en un archivo local aparte de Neon) — y sin la restricción
equivalente en el otro sentido, PendienteSincronizacion/
CredencialOfflineCache (que son exclusivamente locales) terminarían
también migrándose a Neon, donde no tienen ningún sentido.
"""

ALIAS_LOCALES = {"local_disco"}
# Modelos que NUNCA pueden existir en "default" (Neon). Incluye también
# nombres ya retirados: "sesionofflinecache" existió entre el prompt 19 y
# el 19b y sus migraciones (0010 lo crea, 0011 lo borra) se siguen
# replicando tal cual en cualquier base nueva. Si se sacara de esta lista,
# el DROP TABLE de la 0011 correría contra Neon buscando una tabla que —
# correctamente — nunca se creó ahí, y el migrate fallaría.
MODELOS_SOLO_LOCALES = {"pendientesincronizacion", "credencialofflinecache", "sesionofflinecache"}
MODELOS_PERMITIDOS_EN_LOCALES = {"producto", "categoria"} | MODELOS_SOLO_LOCALES


class OfflineRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label != "inventario":
            # auth/sessions/admin/contenttypes, etc.: nunca en los alias
            # locales, siempre lo normal en default.
            return False if db in ALIAS_LOCALES else None

        if db in ALIAS_LOCALES:
            return model_name in MODELOS_PERMITIDOS_EN_LOCALES

        # db == "default" (Neon o el sqlite de desarrollo): todo el app
        # inventario EXCEPTO los dos modelos exclusivamente locales.
        return model_name not in MODELOS_SOLO_LOCALES

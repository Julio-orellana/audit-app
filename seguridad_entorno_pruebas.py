# seguridad_entorno_pruebas.py
"""
Barrera de seguridad contra el error del prompt 30/32: un script pensado
para correr contra una base de datos aislada de pruebas ("test_neondb")
en realidad leyó el `.env` de producción real (nunca se le dijo
explícitamente qué entorno usar, ni se detuvo a confirmar antes de
borrar) y ejecutó `LoteCompra.objects.all().delete()` /
`MovimientoSalida.objects.all().delete()` / `ConteoFisico.objects.all().delete()`
contra datos reales.

La causa raíz nunca fue el script en sí — fue que NINGUNA barrera
estructural impedía que un script de prueba se conectara "a lo que
estuviera activo en ese momento". Este módulo es esa barrera, para
CUALQUIER script (uno del repositorio, o uno improvisado en una sesión
de desarrollo) que vaya a sembrar datos de prueba o borrar registros
masivamente.

Dos herramientas, para dos momentos distintos del ciclo de vida de un
script:

1. cargar_env_de_pruebas() — se llama ANTES de "django.setup()", para
   scripts independientes que arrancan Django ellos mismos (como el que
   causó el incidente). Carga EXPLÍCITAMENTE ".env.test" — nunca el
   ".env" de producción, nunca "lo que ya esté puesto" — y falla fuerte
   si ese archivo no existe o si su DATABASE_URL no tiene pinta de ser
   de pruebas. Ver ".env.test.example" para la plantilla.

2. confirmar_operacion_riesgosa() — se llama justo ANTES de cualquier
   `.delete()` masivo o siembra de datos de prueba, sea en un script
   independiente o en un management command de Django (estos últimos ya
   arrancaron con el .env que sea desde antes — aquí ya no se trata de
   "a qué base conectarse", sino de que SIEMPRE quede explícito a qué
   base se está a punto de escribir, con una confirmación real de por
   medio, salvo automatización ya controlada (forzar=True).

Uso típico en un script independiente:

    import sys
    sys.path.insert(0, "/ruta/al/proyecto")
    from seguridad_entorno_pruebas import cargar_env_de_pruebas, confirmar_operacion_riesgosa

    cargar_env_de_pruebas()          # ANTES de "import django"/"django.setup()"
    import os, django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auditoria_aylupita.settings")
    django.setup()

    from inventario.models import LoteCompra
    confirmar_operacion_riesgosa("borrar TODO LoteCompra")
    LoteCompra.objects.all().delete()

Uso típico en un management command (ver limpiar_datos_prueba.py /
cargar_datos_prueba.py para el patrón real ya aplicado):

    from seguridad_entorno_pruebas import confirmar_operacion_riesgosa

    class Command(BaseCommand):
        def add_arguments(self, parser):
            parser.add_argument("--sin-confirmar", action="store_true", dest="sin_confirmar")

        def handle(self, *args, **options):
            confirmar_operacion_riesgosa("borrar LoteCompra/MovimientoSalida/ConteoFisico",
                                          forzar=options["sin_confirmar"])
            ...
"""
import os
import sys
from pathlib import Path

RAIZ_PROYECTO = Path(__file__).resolve().parent
NOMBRE_ENV_PRODUCCION = ".env"
NOMBRE_ENV_PRUEBAS = ".env.test"


class EntornoNoSeguroError(RuntimeError):
    """
    Se dispara cuando un script no puede confirmar con certeza que está
    en un entorno seguro para la operación que va a hacer — nunca se
    resuelve "asumiendo que sí" ni cayendo de vuelta a producción.
    """


def cargar_env_de_pruebas(nombre_archivo=NOMBRE_ENV_PRUEBAS):
    """
    Carga EXPLÍCITAMENTE un archivo .env de pruebas — nunca el de
    producción — inyectando sus variables directo en os.environ, ANTES
    de que Django (o cualquier código que dependa de
    auditoria_aylupita.settings) se importe.

    Por qué esto SÍ aísla de verdad: django-environ (environ.Env.
    read_env, que settings.py llama con BASE_DIR_ESCRIBIBLE/".env") usa
    os.environ.setdefault() por defecto — nunca pisa una variable que ya
    esté puesta. Si esta función corre PRIMERO y deja DATABASE_URL /
    DIRECT_DATABASE_URL ya puestas en os.environ, lo que settings.py lea
    después de ".env" ya no tiene ningún efecto para esas dos variables:
    gana lo que se cargó aquí, sea cual sea el contenido real del ".env"
    de producción en esa máquina. Esto es lo que el script del incidente
    NUNCA hizo — arrancó Django directo, dejando que settings.py leyera
    lo que hubiera en ".env" sin ninguna instrucción explícita en
    contra.

    Falla fuerte (nunca cae en silencio a producción) si:
    - nombre_archivo apunta literalmente al ".env" de producción,
    - el archivo no existe, o
    - su DATABASE_URL no tiene ninguna señal reconocible de ser de
      pruebas (ni "test" en el nombre de la base, ni localhost/
      127.0.0.1 en el host — el patrón que ya usan tanto "test_neondb"
      como el proxy TCP local usado para simular cortes de red).
    """
    if nombre_archivo == NOMBRE_ENV_PRODUCCION:
        raise EntornoNoSeguroError(
            "cargar_env_de_pruebas() no puede apuntar al .env de producción — "
            "eso es exactamente el bug que causó el incidente del prompt 30/32. "
            "Usa un archivo con otro nombre (ej. .env.test)."
        )

    ruta = RAIZ_PROYECTO / nombre_archivo
    if not ruta.exists():
        raise EntornoNoSeguroError(
            f"No existe {ruta}. Un script de pruebas/siembra/limpieza NUNCA debe "
            f"caer al .env de producción por default — créalo primero a partir de "
            f".env.test.example, con la base de datos de pruebas real."
        )

    import environ

    env = environ.Env()
    # overwrite=True (a diferencia del uso normal en settings.py, que
    # nunca debe pisar nada): aquí SÍ se quiere forzar el valor del
    # archivo de pruebas, para no depender de qué haya quedado puesto en
    # el entorno de una sesión de shell anterior.
    env.read_env(str(ruta), overwrite=True)

    database_url = os.environ.get("DATABASE_URL", "")
    parece_de_pruebas = (
        "test" in database_url.lower()
        or "127.0.0.1" in database_url
        or "localhost" in database_url
    )
    if not parece_de_pruebas:
        raise EntornoNoSeguroError(
            f"{nombre_archivo} tiene un DATABASE_URL que no tiene ninguna señal "
            f"reconocible de ser de pruebas (ni 'test' en el nombre de la base, ni "
            f"localhost/127.0.0.1 en el host) — revísalo a mano antes de continuar. "
            f"Nunca se asume que un archivo es seguro solo por su nombre."
        )

    print(f"[seguridad_entorno_pruebas] Cargado {nombre_archivo} — DATABASE_URL confirmado como de pruebas.")
    return database_url


def resumen_conexion(alias="default"):
    """
    {host, dbname} de la conexión YA resuelta por Django (se llama
    DESPUÉS de django.setup()) — nunca se resume como "conectado" a
    secas, siempre el host y el nombre real de la base, que es
    justamente lo que faltó mostrar antes del incidente.
    """
    from django.conf import settings

    cfg = settings.DATABASES[alias]
    host = cfg.get("HOST") or None
    if not host:
        # SQLite (dev fallback, o los alias "local_disco"/tests): no hay
        # host de red, el "dónde" real es la ruta del archivo.
        return {"host": "(sqlite, sin red)", "dbname": str(cfg.get("NAME"))}
    return {"host": host, "dbname": cfg.get("NAME")}


def confirmar_operacion_riesgosa(descripcion, alias="default", forzar=False):
    """
    Punto de parada obligatorio ANTES de cualquier operación que escriba
    o borre datos de prueba/limpieza a gran escala — un `.delete()`
    masivo, sembrar un mes completo de movimientos falsos, etc.

    Imprime SIEMPRE host + nombre real de la base (nunca "conectado" sin
    más) y exige escribir la palabra "CONFIRMAR" en la consola, salvo
    que forzar=True — pensado para automatización YA controlada (ej. un
    pipeline de CI con su propia base de pruebas dedicada), nunca para
    uso manual cotidiano. Django ya expone esta bandera como
    "--sin-confirmar" en los management commands que la usan.

    Se detiene (EntornoNoSeguroError) si la respuesta no es exactamente
    "CONFIRMAR" — cualquier otra cosa, incluido un Enter vacío, cancela.
    """
    info = resumen_conexion(alias)
    print(f"\n{'=' * 72}")
    print("ATENCIÓN — operación de prueba/limpieza a punto de ejecutarse:")
    print(f"  {descripcion}")
    print(f"  Base de datos: {info['dbname']}")
    print(f"  Host:          {info['host']}")
    print(f"{'=' * 72}")

    if forzar:
        print("(--sin-confirmar activo: se omite la confirmación interactiva)")
        return

    respuesta = input("Escribe CONFIRMAR (en mayúsculas) para continuar, cualquier otra cosa cancela: ")
    if respuesta.strip() != "CONFIRMAR":
        raise EntornoNoSeguroError("Operación cancelada: no se escribió CONFIRMAR.")
    print("Confirmado por el operador — continuando.\n")

# inventario/diagnostico.py
"""
Instrumentación de arranque y de conectividad (prompt 33).

Existe por un problema muy concreto: el motor offline (19/19b/19c)
funciona en Mac pero NO funciona en absoluto en el .exe empaquetado en
Windows, y hasta ahora era imposible saber por qué, porque **en un build
de PyInstaller con `console=False` en Windows el proceso no tiene
consola: `sys.stdout` y `sys.stderr` son None**. Con eso:

- `print(..., file=sys.stderr)` se convierte en un no-op silencioso
  (Python usa sys.stdout cuando file es None, y si ese también es None
  simplemente no escribe nada — no revienta, pero el mensaje se pierde).
- `logging.StreamHandler()` (el que usaba settings.py) escribe a
  `sys.stderr`, o sea a None — `emit()` falla, `logging` se traga el
  error, y no queda rastro de nada.

Resultado: la app podía estar colgada, reventando o escribiendo en una
carpeta equivocada, y en Windows no había ni una línea de log que lo
dijera. Este módulo arregla eso escribiendo SIEMPRE a un archivo junto
al ejecutable (nunca a la consola), y volcando al arrancar todo lo que
hace falta para diagnosticar diferencias de plataforma:

- rutas reales (`sys.executable`, `sys._MEIPASS`, carpeta escribible)
  y una PRUEBA DE ESCRITURA de verdad en cada una,
- la configuración de base de datos que de verdad quedó activa
  (host, OPTIONS, timeouts) — no la que dice el settings.py de
  desarrollo,
- cuánto tarda EXACTAMENTE cada intento de conexión, que es la
  hipótesis principal del prompt 33: sin `connect_timeout`, un corte de
  red real (paquetes que se pierden, no un puerto que rechaza) hace que
  cada intento espere el timeout del sistema operativo — medido en Mac:
  75 segundos por intento — y con waitress corriendo 4 hilos, cuatro
  requests colgados dejan la app entera sin responder.
"""
import logging
import os
import platform
import sys
import time
from pathlib import Path

logger = logging.getLogger("inventario.diagnostico")

NOMBRE_ARCHIVO_LOG = "diagnostico.log"


def ruta_log():
    """El log SIEMPRE junto al ejecutable/proyecto, nunca en el bundle."""
    from runtime_paths import carpeta_escribible

    return carpeta_escribible() / NOMBRE_ARCHIVO_LOG


def _probar_escritura(carpeta):
    """
    Prueba de escritura REAL (crea, escribe, lee y borra un archivo) —
    no basta con `os.access()`, que en Windows miente: informa permiso
    de escritura aunque una ACL, el Control de acceso a carpetas de
    Windows Defender, o una carpeta de solo lectura del sistema lo
    impidan de verdad. Devuelve (ok: bool, detalle: str).
    """
    prueba = Path(carpeta) / ".prueba_escritura_diagnostico"
    try:
        prueba.write_text("ok", encoding="utf-8")
        contenido = prueba.read_text(encoding="utf-8")
        prueba.unlink()
        if contenido != "ok":
            return False, "se escribió pero se leyó distinto"
        return True, "escritura/lectura/borrado OK"
    except Exception as error:
        return False, f"{type(error).__name__}: {error}"


def configurar_log_a_archivo(nivel=logging.INFO):
    """
    Agrega un FileHandler junto al ejecutable a los loggers de la app.
    Idempotente. Se llama lo ANTES posible en el arranque (ver
    app_desktop.py), antes de cualquier cosa que pueda colgarse, para
    que aunque la app se congele después el archivo ya tenga las
    primeras líneas.
    """
    try:
        destino = ruta_log()
        raiz = logging.getLogger("inventario")
        for handler in raiz.handlers:
            if isinstance(handler, logging.FileHandler) and getattr(handler, "_diagnostico", False):
                return destino  # ya configurado

        manejador = logging.FileHandler(destino, mode="a", encoding="utf-8")
        manejador._diagnostico = True
        manejador.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s")
        )
        raiz.addHandler(manejador)
        raiz.setLevel(nivel)
        # propagate=False en los loggers hijos de settings.py haría que
        # nunca llegaran aquí; se fuerza a que sí propaguen al padre.
        for nombre in ("inventario.offline", "inventario.tiempos", "inventario.diagnostico"):
            logging.getLogger(nombre).propagate = True
        return destino
    except Exception:
        # Nunca impedir el arranque por no poder abrir el log.
        return None


def volcar_diagnostico_arranque():
    """
    Vuelca al log todo lo que distingue un build de Windows de uno de
    Mac. Se llama una vez al arrancar, DESPUÉS de django.setup() (para
    poder leer settings) pero ANTES del migrate, que es justo lo que se
    sospecha que cuelga.
    """
    from runtime_paths import carpeta_codigo, carpeta_escribible, esta_empaquetado

    logger.info("=" * 70)
    logger.info("ARRANQUE — diagnóstico de plataforma (prompt 33)")
    logger.info("  plataforma: %s / %s", platform.system(), platform.release())
    logger.info("  python: %s", sys.version.replace("\n", " "))
    logger.info("  empaquetado (sys.frozen): %s", esta_empaquetado())
    logger.info("  sys.executable: %s", sys.executable)
    logger.info("  sys._MEIPASS: %s", getattr(sys, "_MEIPASS", "(no aplica)"))
    logger.info("  sys.argv: %s", sys.argv)
    logger.info("  sys.stdout es None: %s   sys.stderr es None: %s", sys.stdout is None, sys.stderr is None)
    logger.info("  cwd: %s", os.getcwd())

    # --- Rutas de escritura: lo que el prompt 33 pide confirmar ---
    carpeta_cod = carpeta_codigo()
    carpeta_esc = carpeta_escribible()
    logger.info("  carpeta_codigo()     -> %s", carpeta_cod)
    logger.info("  carpeta_escribible() -> %s", carpeta_esc)
    ok, detalle = _probar_escritura(carpeta_esc)
    logger.info("  PRUEBA DE ESCRITURA en carpeta_escribible(): %s — %s", "OK" if ok else "FALLÓ", detalle)
    if not ok:
        logger.error(
            "  *** La app NO puede escribir en su carpeta. Nada se va a guardar: "
            "ni la cola de pendientes, ni el caché de credenciales offline, ni las sesiones. ***"
        )

    from django.conf import settings

    for etiqueta, clave in (("cola/caché offline", "local_disco"),):
        try:
            ruta = Path(settings.DATABASES[clave]["NAME"])
            logger.info("  %s (%s) -> %s  [existe=%s]", etiqueta, clave, ruta, ruta.exists())
        except Exception as error:
            logger.error("  no se pudo resolver la ruta de %s: %s", clave, error)

    try:
        logger.info("  sesiones -> %s  [existe=%s]", settings.SESSION_FILE_PATH, Path(settings.SESSION_FILE_PATH).exists())
    except Exception as error:
        logger.error("  no se pudo resolver SESSION_FILE_PATH: %s", error)

    # --- Configuración REAL de la base (no la del settings.py de dev) ---
    cfg = dict(settings.DATABASES["default"])
    cfg.pop("PASSWORD", None)
    logger.info("  BD engine=%s host=%s name=%s", cfg.get("ENGINE"), cfg.get("HOST"), cfg.get("NAME"))
    logger.info("  BD CONN_MAX_AGE=%s CONN_HEALTH_CHECKS=%s", cfg.get("CONN_MAX_AGE"), cfg.get("CONN_HEALTH_CHECKS"))
    opciones = cfg.get("OPTIONS") or {}
    logger.info("  BD OPTIONS=%s", opciones)
    if "connect_timeout" not in opciones:
        logger.warning(
            "  *** SIN connect_timeout: cada intento de conexión esperará el timeout del "
            "sistema operativo. Con un corte de red real (paquetes perdidos, no un puerto "
            "que rechaza) eso son decenas de segundos por intento — y waitress solo tiene "
            "4 hilos. ***"
        )
    logger.info("=" * 70)


def medir(descripcion):
    """
    Context manager que loguea cuánto tardó un bloque. Se usa para las
    operaciones sospechosas de colgarse (el migrate del arranque, cada
    sondeo de conexión) — la duración medida ES la evidencia que pide el
    prompt 33.
    """
    return _Medicion(descripcion)


class _Medicion:
    def __init__(self, descripcion):
        self.descripcion = descripcion

    def __enter__(self):
        self.inicio = time.monotonic()
        logger.info("INICIO: %s", self.descripcion)
        return self

    def __exit__(self, tipo, valor, tb):
        dur = time.monotonic() - self.inicio
        if tipo is None:
            logger.info("FIN:    %s — %.2fs", self.descripcion, dur)
        else:
            logger.warning("FALLÓ:  %s — %.2fs — %s: %s", self.descripcion, dur, tipo.__name__, valor)
        if dur >= 5:
            logger.error(
                "  *** '%s' tardó %.2fs. Cualquier cosa así de lenta en el camino de una "
                "request deja la app sin responder (waitress: 4 hilos). ***",
                self.descripcion, dur,
            )
        return False  # nunca traga la excepción

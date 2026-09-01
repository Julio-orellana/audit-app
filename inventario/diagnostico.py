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
    El handler de archivo lo declara settings.LOGGING (handler
    "archivo"), NO esta función.

    Antes se agregaba aquí, en tiempo de ejecución, y era un bug:
    get_wsgi_application() vuelve a llamar a django.setup(), que
    re-aplica LOGGING con dictConfig y descarta cualquier handler
    añadido por fuera. Resultado: el archivo solo tenía las líneas del
    arranque y ninguna request ni evento del motor offline — y eso me
    llevó a concluir, equivocadamente, que la app no estaba sirviendo
    páginas.

    Se conserva la función porque app_desktop.py la llama y porque
    devolver la ruta del log es útil, pero ya no toca la configuración.
    """
    return ruta_log()


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

    # --- El archivo .env: existe, y ¿se pudo leer de verdad? ---
    # Son dos cosas distintas y confundirlas cuesta caro: un .env puede
    # estar ahí, verse perfecto al abrirlo, y aun así no aportar nada
    # (ej. guardado con BOM por el Bloc de notas de Windows, que hacía
    # que django-environ descartara la primera línea entera).
    ruta_env = Path(carpeta_esc) / ".env"
    if not ruta_env.exists():
        logger.error("  .env: NO EXISTE en %s — la app no tiene configuración de nube.", ruta_env)
    else:
        try:
            crudo = ruta_env.read_bytes()
            tiene_bom = crudo.startswith(b"\xef\xbb\xbf")
            claves = []
            for linea in crudo.decode("utf-8-sig", errors="replace").splitlines():
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    claves.append(linea.split("=", 1)[0].strip())
            logger.info("  .env: existe (%d bytes), claves definidas: %s", len(crudo), claves or "(ninguna)")
            if tiene_bom:
                logger.warning(
                    "  .env guardado CON BOM (típico del Bloc de notas de Windows). Se lee "
                    "igual porque settings.py usa utf-8-sig, pero si alguna vez ves que la "
                    "PRIMERA variable del archivo 'no existe', esta es la causa."
                )
        except Exception as error:
            logger.error("  .env: existe pero no se pudo leer — %s: %s", type(error).__name__, error)

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

    # --- ¿Configuración de nube ilegible? (prompt 33c) ---
    # Se registra aquí, junto a la configuración real de la base, porque
    # es la diferencia entre dos síntomas que en pantalla se parecen
    # mucho pero se atienden de forma opuesta: "no hay internet" (se
    # arregla solo) vs. "no se pudo leer la configuración" (no se
    # arregla solo, hace falta soporte). El motivo concreto va en la
    # misma línea para no tener que deducirlo del resto del log.
    if getattr(settings, "BD_NUBE_NO_CONFIGURADA", False):
        logger.error(
            "  *** SIN CONFIGURACIÓN DE NUBE UTILIZABLE — motivo: %s. La app abre igual "
            "y opera sobre la cola y el caché local, pero NADA de lo que se registre "
            "llegará al sistema central hasta que se corrija. NO se arregla reconectando "
            "a internet: hace falta colocar/corregir el .env junto al ejecutable. La "
            "interfaz muestra un aviso propio para este caso, distinto del de 'sin "
            "conexión'. ***",
            getattr(settings, "BD_MOTIVO_NO_CONFIGURADA", None) or "(sin detalle)",
        )
    else:
        logger.info("  configuración de nube: legible y utilizable")

    logger.info("=" * 70)


def diagnosticar_conectividad_nube():
    """
    Sonda PROFUNDA, que corre solo cuando la conexión normal ya falló.

    Existe por un caso real de la VM de Windows (prompt 33c) que el log
    no permitía resolver: `Test-NetConnection ... -Port 5432` daba True
    —o sea que el saludo TCP pasaba— y aun así la app fallaba con
    "connection timeout expired" contra las tres IPs, de forma
    reproducible durante más de 10 minutos, mientras el mismo endpoint
    respondía en 0.83s desde otra máquina.

    Con eso quedaban dos explicaciones muy distintas y ninguna forma de
    separarlas desde el log:

      a) la red de esa máquina es LENTA y connect_timeout se queda
         corto — el saludo completo (TCP + TLS + autenticación SCRAM con
         channel binding) son 6-8 viajes de ida y vuelta, así que con
         400ms de latencia se pasa de 3s sin que nada esté roto;
      b) algo BLOQUEA el tráfico cifrado (antivirus, DPI, proxy) — el
         TCP pasa y el handshake nunca termina, tarde lo que tarde.

    Esta función las separa midiendo los tres escalones por separado y
    dándole al último un timeout generoso: si con 25s SÍ conecta, es (a)
    y basta con subir el timeout; si con 25s tampoco, es (b) y no hay
    número que lo arregle.

    Nunca escribe en la base (solo SELECT 1) y nunca registra la
    contraseña.
    """
    import socket

    from django.conf import settings

    cfg = settings.DATABASES["default"]
    host, puerto = cfg.get("HOST"), int(cfg.get("PORT") or 5432)
    if not host or "postgresql" not in (cfg.get("ENGINE") or ""):
        return

    logger.info("-" * 70)
    logger.info("SONDA DE CONECTIVIDAD (la conexión normal falló — prompt 33c)")

    # --- Escalón 1: DNS ---
    inicio = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, puerto, type=socket.SOCK_STREAM)
        direcciones = sorted({i[4][0] for i in infos})
        logger.info(
            "  1. DNS: %d dirección(es) en %.2fs -> %s",
            len(direcciones), time.monotonic() - inicio, direcciones,
        )
        logger.info(
            "     OJO: connect_timeout de libpq es POR DIRECCIÓN, no total. "
            "Con %d direcciones y connect_timeout=%s, un corte real cuesta "
            "hasta %ss antes de rendirse.",
            len(direcciones), (cfg.get("OPTIONS") or {}).get("connect_timeout", "?"),
            len(direcciones) * int((cfg.get("OPTIONS") or {}).get("connect_timeout", 0) or 0),
        )
    except Exception as error:
        logger.error("  1. DNS: FALLÓ en %.2fs — %s: %s", time.monotonic() - inicio, type(error).__name__, error)
        logger.info("     Sin DNS no hay nada más que probar: el equipo no resuelve el nombre.")
        logger.info("-" * 70)
        return

    # --- Escalón 2: TCP puro, por dirección ---
    algun_tcp = False
    for direccion in direcciones:
        familia = socket.AF_INET6 if ":" in direccion else socket.AF_INET
        sock = socket.socket(familia, socket.SOCK_STREAM)
        sock.settimeout(5)
        inicio = time.monotonic()
        try:
            sock.connect((direccion, puerto))
            algun_tcp = True
            resultado = "OK"
        except Exception as error:
            resultado = f"{type(error).__name__}: {error}"
        finally:
            sock.close()
        logger.info("  2. TCP %s -> %s (%.2fs)", direccion, resultado, time.monotonic() - inicio)

    # Sin TCP a ninguna dirección no tiene sentido el escalón 3: se
    # pasaría 25s esperando para confirmar lo que ya se sabe. Este es
    # además el caso MÁS COMÚN (probar la app con la red apagada a
    # propósito), así que ahorrarse esa espera importa.
    if not algun_tcp:
        logger.error(
            "  *** VEREDICTO: no hay ni TCP a ninguna dirección. El equipo no está llegando "
            "a la nube: adaptador de red apagado, sin internet, o el puerto 5432 bloqueado "
            "de salida. (Si apagaste la red a propósito para probar el modo offline, esto es "
            "justo lo esperado.) ***"
        )
        logger.info("-" * 70)
        return

    # --- Escalón 3: escalera de intentos, quitando cosas de a una ---
    # Se prueban variantes de MENOS a MÁS restrictivo para que el propio
    # log diga cuál es el ingrediente que molesta, en vez de dejar el
    # diagnóstico en "algo de la red" (prompt 33c).
    #
    # Se fija hostaddr a UNA sola dirección (manteniendo host, que Neon
    # necesita para enrutar por SNI) para que cada intento cueste un
    # timeout y no el timeout x cantidad de direcciones.
    #
    # El último intento, sin cifrado, es el que más informa: Neon EXIGE
    # TLS, así que va a rechazarlo — pero un rechazo DEL SERVIDOR prueba
    # que el camino de red funciona y que lo que se está bloqueando es
    # concretamente el tráfico cifrado. Si en cambio también se cuelga o
    # da "Permission denied", el bloqueo es de todo el puerto 5432.
    import psycopg

    base = dict(cfg.get("OPTIONS") or {})
    direccion = next((d for d in direcciones if ":" not in d), direcciones[0])

    def _sin(*claves):
        return {k: v for k, v in base.items() if k not in claves}

    intentos = [
        ("opciones completas (las que usa la app)", base),
        ("sin tcp_user_timeout", _sin("tcp_user_timeout")),
        ("sin keepalives ni tcp_user_timeout", _sin(
            "tcp_user_timeout", "keepalives", "keepalives_idle",
            "keepalives_interval", "keepalives_count")),
        ("mínimo: solo sslmode=require", {"sslmode": "require"}),
        ("SIN cifrado (sslmode=disable)", {"sslmode": "disable"}),
    ]

    alguno_funciono = False
    bloqueo_de_windows = False
    for etiqueta, opciones in intentos:
        opciones = dict(opciones)
        opciones.pop("connect_timeout", None)
        inicio = time.monotonic()
        try:
            conexion = psycopg.connect(
                host=host, hostaddr=direccion, port=puerto, dbname=cfg.get("NAME"),
                user=cfg.get("USER"), password=cfg.get("PASSWORD"),
                connect_timeout=8, **opciones
            )
            with conexion.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            conexion.close()
            alguno_funciono = True
            logger.error(
                "  3. %-42s -> CONECTÓ en %.2fs  <-- ESTE FUNCIONA",
                etiqueta, time.monotonic() - inicio,
            )
        except Exception as error:
            texto = " ".join(str(error).split())
            if "10013" in texto or "0x0000271D" in texto or "Permission denied" in texto:
                bloqueo_de_windows = True
            logger.info("  3. %-42s -> falló en %.2fs — %s", etiqueta, time.monotonic() - inicio, texto[:200])

    # --- Veredicto ---
    if alguno_funciono:
        logger.error(
            "  *** VEREDICTO: al menos una variante SÍ conecta. La línea marcada con "
            "'ESTE FUNCIONA' dice qué combinación sirve en este equipo — mándame el log y "
            "ajusto la configuración de la app para usarla. ***"
        )
    elif bloqueo_de_windows:
        logger.error(
            "  *** VEREDICTO: Windows está NEGANDO la conexión (error 10013 / WSAEACCES). "
            "Eso no es la red ni la nube ni el timeout: es software de ESTE equipo "
            "bloqueando el socket de salida. Sospechosos, en orden: (1) antivirus o "
            "seguridad con inspección de tráfico —el TCP simple pasa y la conexión cifrada "
            "se corta, que es justo el patrón de este log—; (2) una regla de salida del "
            "Firewall de Windows sobre el puerto 5432; (3) rangos de puertos reservados por "
            "Hyper-V/WSL/Docker (comprobar con: netsh int ipv4 show excludedportrange tcp). "
            "Ninguna se arregla desde la app. ***"
        )
    else:
        logger.error(
            "  *** VEREDICTO: ninguna variante conecta y no es un bloqueo explícito de "
            "Windows. El TCP pasa pero el saludo no termina: algo descarta el tráfico en "
            "silencio (proxy, DPI, filtrado del hipervisor). Prueba la app en otra red. ***"
        )
    logger.info("-" * 70)


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

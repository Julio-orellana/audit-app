# app_desktop.py
"""
Punto de entrada de la app de escritorio: levanta el proyecto Django con
waitress en 127.0.0.1 (nunca en 0.0.0.0 — nunca se expone a la red) y lo
muestra en una ventana pywebview propia, sin barra de direcciones ni menú
de navegador.

El servidor corre en un hilo daemon dentro del mismo proceso. webview.start()
bloquea el hilo principal hasta que el usuario cierra la ventana; al volver,
el proceso termina (sys.exit) y el hilo daemon muere con él, liberando el
puerto por completo — no queda ningún proceso de Python en segundo plano.
"""
import os
import socket
import sys
import threading
import time

HOST = "127.0.0.1"  # nunca "0.0.0.0": la app nunca debe ser alcanzable desde la red
PORT = 8000


def _preparar_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auditoria_aylupita.settings")
    import django

    django.setup()

    # Instrumentación de arranque (prompt 33) — lo PRIMERO después de
    # django.setup(), antes de cualquier cosa que pueda colgarse. En un
    # build de Windows con console=False no hay stdout/stderr, así que
    # sin esto no queda absolutamente ningún rastro de lo que pasó: el
    # log va a un archivo junto al ejecutable.
    from inventario.diagnostico import (
        configurar_log_a_archivo,
        medir,
        volcar_diagnostico_arranque,
    )

    configurar_log_a_archivo()
    volcar_diagnostico_arranque()

    # Hilo de fondo que sube la cola de pendientes (prompt 19). Va ANTES
    # del migrate de "default" (Neon) y no depende para nada de que ese
    # migrate funcione: la base local es SQLite, siempre disponible. Esto
    # es justo lo que permite arrancar la app sin conexión.
    #
    # Desde el prompt 19b esto también lo arranca InventarioConfig.ready()
    # (para que `manage.py runserver` sincronice igual); la llamada es
    # idempotente, así que dejarla aquí explícita no arranca un segundo
    # hilo — solo documenta que la app de escritorio lo necesita sí o sí.
    from inventario.offline import iniciar_hilo_sincronizacion

    iniciar_hilo_sincronizacion()

    # Aplica migraciones pendientes en cada arranque contra Neon. Es
    # barato y no hace nada si ya están aplicadas (comando idempotente) —
    # pero es lo que permite que el .exe funcione con un doble clic en
    # una máquina nueva, sin que nadie tenga que abrir una terminal y
    # correr "manage.py migrate" a mano la primera vez.
    #
    # Envuelto en try/except (prompt 19): antes, si la app arrancaba sin
    # conexión, esto reventaba sin capturar el error y la app ni
    # siquiera llegaba a abrir la ventana — confirmado con una prueba
    # real cortando la conexión a propósito. Una migración pendiente en
    # un arranque cualquiera es rarísima (solo pasa justo después de
    # actualizar la app); no vale la pena bloquear TODO el arranque por
    # eso — si de verdad hace falta, se aplica sola la próxima vez que
    # haya conexión al abrir la app.
    from django.core.management import call_command
    from django.db.utils import InterfaceError, OperationalError

    # Envuelto en medir() (prompt 33): este migrate es el principal
    # sospechoso de que el .exe "no abra" sin conexión en Windows —
    # bloquea el arranque ANTES de que se cree la ventana, así que si
    # tarda decenas de segundos el usuario solo ve que no pasa nada. La
    # duración queda registrada en el log aunque no haya consola.
    import logging

    from django.conf import settings

    log_arranque = logging.getLogger("inventario.diagnostico")
    if getattr(settings, "BD_NUBE_NO_CONFIGURADA", False):
        # Sin configuración de nube no hay nada que migrar y no vale la
        # pena ni intentar la conexión (prompt 33). La app arranca igual
        # y opera sobre la cola/caché local — el aviso diferenciado se lo
        # da la interfaz al usuario, no un fallo de arranque.
        log_arranque.error(
            "NO se pudo leer la configuración de la base de datos en la nube "
            "(falta el archivo .env junto al ejecutable, o su DATABASE_URL es "
            "inválida). La app arranca en modo local: todo lo que se registre "
            "queda en la cola de este equipo y NO llegará al sistema central "
            "hasta que se corrija la configuración. Esto NO se arregla "
            "reconectando a internet. Motivo detectado: %s",
            getattr(settings, "BD_MOTIVO_NO_CONFIGURADA", None) or "(sin detalle)",
        )
        from django.core.wsgi import get_wsgi_application

        return get_wsgi_application()

    try:
        with medir("migrate contra la base en la nube (bloquea el arranque)"):
            call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)
    except (OperationalError, InterfaceError):
        # print(file=sys.stderr) NO sirve en un build de Windows con
        # console=False: sys.stderr es None y print() se vuelve un no-op
        # silencioso (comprobado). Por eso esto va al log de archivo.
        log_arranque.warning(
            "Sin conexión a la base en la nube al arrancar — se sigue de "
            "todas formas con el catálogo y la cola guardados en este "
            "equipo. Los movimientos se sincronizarán solos en cuanto "
            "vuelva internet."
        )
        # Solo cuando ya falló: separa "la red es lenta y el timeout se
        # quedó corto" de "algo bloquea el TLS" — dos causas que en el
        # log se veían idénticas y se arreglan de formas opuestas
        # (prompt 33c). Cuesta unos segundos, pero únicamente en el
        # arranque que ya falló, nunca en el camino normal.
        try:
            from inventario.diagnostico import diagnosticar_conectividad_nube

            diagnosticar_conectividad_nube()
        except Exception:
            log_arranque.exception("La sonda de conectividad falló (no impide arrancar).")

    from django.core.wsgi import get_wsgi_application

    return get_wsgi_application()


def _servir(application):
    from waitress import serve

    serve(application, host=HOST, port=PORT)


def _esperar_servidor(host, port, intentos=150, intervalo=0.1):
    """
    Espera a que el servidor acepte conexiones TCP en (host, port), con
    reintentos cortos en vez de un sleep fijo arbitrario. ~15s de margen
    total (150 x 0.1s) antes de rendirse.
    """
    for _ in range(intentos):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(intervalo)
    return False


def main():
    from backup_db import hacer_backup

    hacer_backup()

    application = _preparar_django()

    hilo_servidor = threading.Thread(target=_servir, args=(application,), daemon=True)
    hilo_servidor.start()

    if not _esperar_servidor(HOST, PORT):
        print(f"No se pudo conectar a http://{HOST}:{PORT} — revisa el log de arriba.", file=sys.stderr)
        sys.exit(1)

    import webview

    # Por defecto pywebview bloquea las descargas de archivos iniciadas por
    # la página (webview.settings["ALLOW_DOWNLOADS"] = False) — sin esto,
    # el botón "Descargar Excel" no hace nada dentro de la ventana de
    # escritorio, aunque funcione perfecto en un navegador normal.
    webview.settings["ALLOW_DOWNLOADS"] = True

    webview.create_window(
        "Auditoría Aylupita",
        f"http://{HOST}:{PORT}",
        width=1280,
        height=800,
    )
    webview.start()

    # webview.start() regresa cuando el usuario cierra la ventana.
    # El hilo del servidor es daemon: al salir del proceso aquí, muere con
    # él y el sistema operativo libera el puerto inmediatamente.
    sys.exit(0)


if __name__ == "__main__":
    main()

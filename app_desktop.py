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
import sys2
import threading
import time

HOST = "127.0.0.1"  # nunca "0.0.0.0": la app nunca debe ser alcanzable desde la red
PORT = 8000


def _preparar_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auditoria_aylupita.settings")
    import django

    django.setup()

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

    try:
        call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)
    except (OperationalError, InterfaceError):
        print(
            "Sin conexión a la base en la nube al arrancar — se sigue de "
            "todas formas con el catálogo y la cola guardados en este "
            "equipo. Los movimientos se sincronizarán solos en cuanto "
            "vuelva internet.",
            file=sys.stderr,
        )

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

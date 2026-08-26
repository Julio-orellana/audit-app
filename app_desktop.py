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

    # Aplica migraciones pendientes en cada arranque. Es barato y no hace
    # nada si ya están aplicadas (comando idempotente) — pero es lo que
    # permite que el .exe funcione con un doble clic en una máquina nueva,
    # sin que nadie tenga que abrir una terminal y correr
    # "manage.py migrate" a mano la primera vez.
    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)

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

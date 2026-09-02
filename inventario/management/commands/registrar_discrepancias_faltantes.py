"""
Crea las discrepancias de los conteos que quedaron sin registro.

POR QUÉ EXISTE (prompt 37)

Hasta el prompt 34, la alerta de "este conteo no cuadra" se calculaba en
vivo: literalmente `if diferencia != 0` cada vez que se dibujaba el
tablero. El prompt 34 cambió eso —con razón, porque una alerta calculada
en vivo desaparecía sola cuando un movimiento posterior hacía cuadrar los
números, sin que nadie la resolviera— y ahora cada conteo con diferencia
genera un DiscrepanciaInventario que solo cambia porque una persona lo
cambió.

Lo que faltó fue el paso de en medio: los conteos que YA existían cuando
llegó el cambio nunca recibieron su registro, porque la discrepancia la
crea una señal post_save y esos conteos ya estaban guardados. El
resultado es silencioso y por eso peligroso: el tablero deja de avisar de
diferencias reales que están ahí, y la pantalla de Diferencias sale
vacía. Al detectarlo en producción, los 9 conteos existentes tenían
diferencia y ninguno tenía registro.

POR QUÉ ES UN COMANDO Y NO UNA MIGRACIÓN

Una migración de datos la aplicaría `migrate`, y el programa de
escritorio corre `migrate` solo al arrancar: la primera persona que
abriera el .exe habría escrito en la base de producción sin decidirlo ni
enterarse. Esto tiene que ser una decisión consciente, con la base a la
vista y confirmación explícita — el mismo resguardo que el resto de
operaciones delicadas del proyecto (ver DESARROLLO.md).

Es idempotente: un conteo que ya tiene su discrepancia se salta, así que
correrlo dos veces no duplica nada. Con --dry-run no escribe nada.
"""
from django.core.management.base import BaseCommand

from ...discrepancias import registrar_discrepancia
from ...models import ConteoFisico, DiscrepanciaInventario


class Command(BaseCommand):
    help = (
        "Crea el registro de discrepancia de los conteos físicos que tienen "
        "diferencia pero se guardaron antes de que existieran las discrepancias."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Muestra qué se crearía sin escribir absolutamente nada.",
        )
        parser.add_argument(
            "--sin-confirmar", action="store_true",
            help="Omite la confirmación interactiva. Solo para automatización controlada.",
        )

    def handle(self, *args, **opciones):
        seco = opciones["dry_run"]

        ya_tienen = set(DiscrepanciaInventario.objects.values_list("conteo_id", flat=True))
        pendientes = []
        for conteo in ConteoFisico.objects.select_related("producto").order_by("fecha", "ocurrido_en"):
            if conteo.pk in ya_tienen:
                continue
            teorico = conteo.producto.stock_teorico(
                hasta_fecha=conteo.fecha, hasta_instante=conteo.ocurrido_en
            )
            diferencia = conteo.cantidad_contada - teorico
            if diferencia == 0:
                continue          # cuadró: no hay discrepancia que registrar
            pendientes.append((conteo, teorico, diferencia))

        if not pendientes:
            self.stdout.write(self.style.SUCCESS(
                "Nada que hacer: todos los conteos con diferencia ya tienen su registro."
            ))
            return

        self.stdout.write(f"\nConteos sin registro de discrepancia: {len(pendientes)}\n")
        for conteo, teorico, diferencia in pendientes:
            signo = "faltan" if diferencia < 0 else "sobran"
            self.stdout.write(
                f"  #{conteo.pk:<4} {conteo.fecha}  {conteo.producto.nombre:<24} "
                f"contado={conteo.cantidad_contada:<7} teórico={teorico:<7} "
                f"{signo} {abs(diferencia)}"
            )

        if seco:
            self.stdout.write(self.style.WARNING(
                "\n--dry-run: no se escribió nada. Quita la bandera para aplicarlo."
            ))
            return

        # Punto de parada obligatorio: imprime host y nombre reales de la
        # base y exige escribir CONFIRMAR (ver DESARROLLO.md, sección de
        # scripts que escriben). Esta operación normalmente corre contra
        # PRODUCCIÓN, que es justo el motivo por el que no puede ser
        # silenciosa.
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
        from seguridad_entorno_pruebas import EntornoNoSeguroError, confirmar_operacion_riesgosa

        try:
            confirmar_operacion_riesgosa(
                f"Crear {len(pendientes)} registro(s) de discrepancia para conteos ya existentes.",
                forzar=opciones["sin_confirmar"],
            )
        except EntornoNoSeguroError as error:
            self.stdout.write(self.style.ERROR(f"\n{error}"))
            return

        creadas = 0
        for conteo, _, _ in pendientes:
            if registrar_discrepancia(conteo) is not None:
                creadas += 1
        self.stdout.write(self.style.SUCCESS(
            f"\nListo: {creadas} discrepancia(s) creada(s). Quedan pendientes de que una "
            f"persona las revise en la pantalla de Diferencias — no se aplicó ningún ajuste."
        ))

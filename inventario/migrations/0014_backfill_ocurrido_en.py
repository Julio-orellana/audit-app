# Generated for prompt 34.
from django.db import migrations


def rellenar_ocurrido_en(apps, schema_editor):
    """
    Da un instante a los registros que ya existían (prompt 34).

    Lo mejor disponible es `creado_en`, que es la hora del INSERT: para lo
    que se registró CON conexión es exacta, y para lo que llegó por
    sincronización es la hora de llegada, no la real. Se acepta esa
    imprecisión a propósito — la columna no admite nulos y el orden
    aproximado de datos históricos es mejor que ninguno.

    Lo que NO hace esta migración, por decisión explícita: crear
    DiscrepanciaInventario para los conteos ya existentes. Su ancla sería
    aproximada y las cifras congeladas saldrían de un pasado que no se
    puede reconstruir. Los conteos viejos se quedan como están.
    """
    for nombre in ("LoteCompra", "MovimientoSalida", "ConteoFisico"):
        Modelo = apps.get_model("inventario", nombre)
        # F() en vez de traer las filas: son tablas que pueden tener miles
        # de registros y esto corre dentro de la transacción de migración.
        from django.db.models import F

        Modelo.objects.filter(ocurrido_en__isnull=True).update(ocurrido_en=F("creado_en"))


def revertir(apps, schema_editor):
    # Volver a dejarlos en NULL es exactamente el estado previo.
    for nombre in ("LoteCompra", "MovimientoSalida", "ConteoFisico"):
        Modelo = apps.get_model("inventario", nombre)
        Modelo.objects.update(ocurrido_en=None)


class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0013_ancla_temporal_y_discrepancias"),
    ]

    operations = [
        migrations.RunPython(rellenar_ocurrido_en, revertir),
    ]

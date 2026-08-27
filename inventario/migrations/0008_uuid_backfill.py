# Paso 2 de 3 (prompt 19): asigna un uuid.uuid4() distinto a cada fila que
# ya existía antes de este prompt (las filas nuevas ya reciben uno único
# solas, vía el default del campo).
import uuid

from django.db import migrations


def backfill(apps, schema_editor):
    for nombre_modelo in ("LoteCompra", "MovimientoSalida", "ConteoFisico"):
        Modelo = apps.get_model("inventario", nombre_modelo)
        for fila in Modelo.objects.filter(uuid__isnull=True).only("pk"):
            fila.uuid = uuid.uuid4()
            fila.save(update_fields=["uuid"])


def revertir(apps, schema_editor):
    # No hay nada que deshacer: volver a null es responsabilidad de 0007
    # al revertirse, no de este paso.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0007_uuid_add_nullable'),
    ]

    operations = [
        migrations.RunPython(backfill, revertir),
    ]

# Paso 1 de 3 para agregar "uuid" a LoteCompra/MovimientoSalida/ConteoFisico
# (prompt 19): se agrega nullable y SIN unique todavía — Django evalúa un
# default callable (uuid.uuid4) UNA sola vez para todo el ALTER TABLE en
# Postgres (no una vez por fila), así que agregarlo ya con unique=True
# aquí choca de inmediato (todas las filas existentes comparten el mismo
# valor). El paso 2 rellena un valor de verdad único por fila; el paso 3
# recién ahí agrega la restricción unique.
# Ver: https://docs.djangoproject.com/en/6.1/howto/writing-migrations/#migrations-that-add-unique-fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0006_ajuste_generado_unico'),
    ]

    operations = [
        migrations.AddField(
            model_name='lotecompra',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name='movimientosalida',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name='conteofisico',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
    ]

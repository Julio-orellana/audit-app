# Paso 3 de 3 (prompt 19): ya con todas las filas rellenadas (0008), se
# cierra la puerta a null — deja el campo exactamente como está declarado
# en models.py (default=uuid.uuid4, unique=True, sin null).
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0008_uuid_backfill'),
    ]

    operations = [
        migrations.AlterField(
            model_name='lotecompra',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name='movimientosalida',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name='conteofisico',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

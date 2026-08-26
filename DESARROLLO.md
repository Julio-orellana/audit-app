# Desarrollo

Instrucciones rápidas para trabajar en el proyecto. Para compilar el
`.exe` de escritorio, ver [EMPAQUETADO.md](EMPAQUETADO.md).

## Base de datos

Por defecto, si `.env` no tiene `DATABASE_URL`, el proyecto usa SQLite
local (`db.sqlite3`) — no hace falta nada más para desarrollar. Para usar
la base real en Neon (Postgres), completa `.env` a partir de
`.env.example` (ver prompt 18).

## Correr las pruebas

```bash
python manage.py test
```

Corriendo contra Neon, este comando usa **`--keepdb` por defecto**
(sobreescrito en `inventario/management/commands/test.py`, prompt 18b) —
sin esto, `manage.py test` normal falla al intentar borrar la base de
pruebas al final, porque el pooler de Neon no siempre libera la conexión
lo bastante rápido a tiempo para el `DROP DATABASE`. Con `--keepdb`,
Django crea la base de pruebas una sola vez (`test_<nombre de la base
real>` — nunca toca los datos reales) y la reutiliza en cada corrida,
aplicando cualquier migración pendiente si hace falta, sin intentar
borrarla nunca.

Si alguna vez hace falta una base de pruebas nueva desde cero (ej. las
migraciones cambiaron de forma incompatible con la que ya existe):
`--keepdb` no tiene un flag opuesto en Django para "apagarlo" desde la
terminal — hay que borrar la base `test_<nombre>` a mano desde la
consola de Neon. Correr las pruebas contra SQLite local sigue funcionando
igual, moviendo `.env` a un lado temporalmente:

```bash
mv .env .env.bak && python manage.py test; mv .env.bak .env
```

## Restaurar desde un respaldo completo (prompt 23)

Cada login de un admin/auditor (nunca vendedor) genera, como máximo una
vez por día, un volcado completo de la base de datos en
`backups_completos/respaldo_completo_<fecha>_<hora>.json`
(`inventario/management/commands/respaldar_bd_completa.py`). Esto es
**solo para ti** — el manual del usuario final (`MANUAL_USUARIO.pdf`)
explica qué es la carpeta en términos simples, pero no el procedimiento
de restauración en sí, que es una operación técnica.

Para restaurar sobre una base VACÍA y recién migrada (nunca sobre una que
ya tenga datos — `loaddata` no reemplaza filas existentes, las duplica o
choca con ellas):

```bash
python manage.py migrate
python manage.py loaddata backups_completos/respaldo_completo_2026-08-26_15-10-20.json
```

Notas:
- El dump se genera con `--natural-foreign --natural-primary`, así que
  `loaddata` resuelve `auth.User`/`auth.Group` por username/nombre en vez
  de por id — no hay riesgo de colisión de ids con lo que `migrate` ya
  haya creado en la base destino.
- Se excluyen a propósito `contenttypes`, `auth.permission`,
  `sessions.session` y `admin.logentry` del dump — son contenido que
  Django ya recrea solo al correr `migrate`, no datos reales del negocio;
  intentar restaurarlos podría chocar con lo que la base destino ya
  tiene.
- Verificado en este mismo prompt: `loaddata` sobre una base SQLite
  local, vacía y recién migrada, restauró los 26 productos, 2
  LoteCompra, 13 MovimientoSalida, 5 ConteoFisico, 3 usuarios y sus 3
  grupos exactamente — incluido un `stock_teorico()` calculado
  correctamente después de la restauración.
- Si vas a restaurar sobre Neon (no solo probar en local), apunta
  `DATABASE_URL`/`DIRECT_DATABASE_URL` a la base de destino en `.env`
  antes de correr los comandos de arriba — igual que cualquier otro uso
  de `manage.py` contra la nube.

## Servidor de desarrollo

```bash
python manage.py runserver
```

O `python app_desktop.py` para probar la app tal como la usa el auditor
(ventana de escritorio con pywebview, sin barra de direcciones).

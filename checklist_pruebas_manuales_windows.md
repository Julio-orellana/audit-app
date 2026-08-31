# Checklist de pruebas manuales en Windows — motor offline (prompt 33)

Esta lista es para correrla a mano en la VM de Windows. Ve marcando cada
punto; si algo falla, anota **qué pasó exactamente** (mensaje, pantalla,
comportamiento) y adjunta el `diagnostico.log`.

---

## Preparación (esto es lo que estaba fallando — no lo saltes)

```
git pull
build_exe.bat
```

Luego, **el paso crítico**: copia tu `.env` real (el que tiene
`DATABASE_URL` apuntando a Neon) **dentro de `dist\AuditoriaAylupita\`,
junto al `.exe`**.

> Ese archivo faltando fue la causa raíz de que todo el motor offline
> estuviera muerto en Windows: sin él, la app se conectaba a un SQLite
> local vacío en vez de a Neon, y como un archivo local "siempre
> conecta", el modo offline nunca se activaba. Ojo: la instrucción vieja
> (prompt 13b) decía copiar `db.sqlite3`; **eso ya no aplica**, ahora es
> el `.env`.

- [ ] `.env` copiado junto al `.exe`.
- [ ] Borra el `db.sqlite3` que haya quedado junto al `.exe` de pruebas
      anteriores (es la base huérfana del bug; ya no se usa).

### Punto 0 — confirmar que la configuración se leyó bien

Abre el `.exe` una vez **con internet**, ciérralo, y abre el archivo
`diagnostico.log` que queda junto al `.exe`.

- [ ] Dice `BD engine=django.db.backends.postgresql` y
      `host=ep-aged-frog-...neon.tech` — **NO** `sqlite3`.
- [ ] Dice `BD OPTIONS={... 'connect_timeout': 10, 'keepalives': 1 ...}`.
- [ ] **NO** aparece la línea `NO se pudo leer la configuración de la base de datos`.
- [ ] `PRUEBA DE ESCRITURA en carpeta_escribible(): OK`.

Si algo de esto falla, **detente aquí** y mándame el log: los 6 puntos de
abajo no tienen sentido hasta que esto esté bien.

---

## Los 6 puntos

### 1. Abrir el `.exe` SIN conexión y entrar con los tres roles

Desactiva el adaptador de red de la VM **antes** de abrir la app.

- [ ] El `.exe` abre (la ventana aparece; no se queda colgada ni tarda
      medio minuto).
- [ ] `Ruth` inicia sesión.
- [ ] `Mich2026` inicia sesión.
- [ ] `ventas` inicia sesión.

> Requisito: cada usuario debe haber iniciado sesión **con internet al
> menos una vez en esa máquina** antes, para que su credencial esté
> cacheada. Si nunca entró ahí, no poder entrar sin red es correcto.

### 2. Cortar la red con la app ya abierta, en la pantalla de login

- [ ] Abre la app **con** internet, quédate en la pantalla de login.
- [ ] Desactiva el adaptador de red.
- [ ] Inicia sesión — debe funcionar igual, sin congelarse.
- [ ] La ventana sigue respondiendo (los botones y enlaces reaccionan).

### 3. Registrar un movimiento offline con cada rol

Con la red aún cortada:

- [ ] `ventas`: registra una venta. La fecha queda fija en hoy, sin poder
      elegirla.
- [ ] `Mich2026`: registra una entrada (o merma, o conteo físico).
- [ ] `Ruth`: registra otro movimiento.
- [ ] Aparece el indicador **"N pendientes de sincronizar"** en la barra
      superior, y el número sube con cada movimiento.
- [ ] En la sección **Sincronización** se ven los movimientos listados con
      tipo, producto, cantidad y fecha de creación.

### 4. Cierre forzado con un pendiente, y reabrir sin conexión

Con la red aún cortada y pendientes sin sincronizar:

- [ ] Cierra el proceso **a la fuerza** desde el Administrador de tareas
      (finalizar tarea), no con la X de la ventana.
- [ ] Vuelve a abrir el `.exe`, **sin reconectar la red**.
- [ ] Los pendientes **siguen ahí** (mismo número en el indicador y en la
      pantalla de Sincronización).

### 5. Reconectar y confirmar que sincroniza sola

- [ ] Reactiva el adaptador de red. **No toques nada más en la app.**
- [ ] En menos de ~1 minuto, el indicador de pendientes baja a 0 solo.
- [ ] En `diagnostico.log` aparece `Sincronizado ... contra Neon`.
- [ ] Los movimientos aparecen en Historial y afectan el stock.

### 6. Navegar offline por todo el navbar, con cada rol

Con la red cortada, entra a **cada** sección de la barra superior, una
por una, con cada rol. Ninguna debe dejar la app insensible.

Deben **funcionar**:
- [ ] Inicio (catálogo cacheado)
- [ ] Historial (lectura; los pendientes salen marcados)
- [ ] Sincronización
- [ ] Instrucciones
- [ ] Registrar venta / entrada / merma / conteo

Deben mostrar **el mensaje claro** de "requiere conexión" (no un error
técnico, no congelarse):
- [ ] Reportes
- [ ] Categorías
- [ ] Productos
- [ ] Correcciones
- [ ] Editar o eliminar un registro desde Historial

Y con `ventas`:
- [ ] No puede entrar a entradas, mermas, ajustes, conteo físico ni
      reportes (bloqueo por rol, no un crash).

---

## Qué mandarme

1. El archivo **`diagnostico.log`** completo (junto al `.exe`) — es lo más
   importante. Ahora sí registra cada request y cada sondeo de conexión.
2. Qué puntos fallaron y qué viste exactamente.
3. Si algo se congeló: cuántos segundos aproximadamente, y en qué
   pantalla.

### Cómo se ve un problema en el log

- `hay_conexion() tardó XX.XXs` → sigue habiendo un bloqueo por red
  (justo lo que el `connect_timeout` debía evitar).
- `PRUEBA DE ESCRITURA ... FALLÓ` → la app no puede escribir junto al
  `.exe` (permisos de Windows, o Control de acceso a carpetas de
  Windows Defender). Nada se guardaría.
- `NO se pudo leer la configuración de la base de datos` → falta el
  `.env`, volver a la Preparación.

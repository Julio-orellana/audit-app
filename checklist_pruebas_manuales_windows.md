# Checklist de pruebas manuales en Windows — motor offline (prompts 33 / 33b / 33c)

Esta lista es para correrla a mano en la VM de Windows. Ve marcando cada
punto; si algo falla, anota **qué pasó exactamente** (mensaje, pantalla,
comportamiento) y adjunta el `diagnostico.log`.

---

## Preparación (esto es lo que estaba fallando — no lo saltes)

```
git pull
build_exe.bat
```

> **Cada cambio de código exige recompilar.** El `.exe` corre desde su
> propia copia empaquetada, no desde el código fuente: un `git pull` solo
> no cambia nada de lo que ves en la app.
>
> `build_exe.bat` ahora **respalda y restaura solo** el `.env`, la cola
> local (`offline_local.sqlite3`), las sesiones y los respaldos — porque
> PyInstaller borra la carpeta de salida entera antes de reconstruirla.
> Si compilas de otra forma (`pyinstaller app.spec` a mano), esos
> archivos **se pierden** y hay que volver a copiar el `.env` y a repetir
> el Punto 0-bis.

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
- [ ] Dice `BD OPTIONS={... 'connect_timeout': 3, 'keepalives': 1, 'keepalives_idle': 10 ...}`.
- [ ] Dice `configuración de nube: legible y utilizable`.
- [ ] **NO** aparece la línea `NO se pudo leer la configuración de la base de datos`.
- [ ] `PRUEBA DE ESCRITURA en carpeta_escribible(): OK`.

Si algo de esto falla, **detente aquí** y mándame el log: los puntos de
abajo no tienen sentido hasta que esto esté bien.

#### Si dice `FALLÓ: migrate ... connection timeout expired` aunque haya internet

Desde el prompt 33c, cuando la conexión del arranque falla la app corre
sola una **sonda de conectividad** y escribe en el log un `VEREDICTO`.
Búscalo, porque distingue dos causas que se ven idénticas y se arreglan
al revés:

- *"la nube SÍ responde, pero el saludo completo tarda X s"* → la red de
  ese equipo es lenta y el timeout se quedó corto. **No hay que
  recompilar**: agrega `DB_CONNECT_TIMEOUT=<el número que sugiere el
  log>` al `.env` que está junto al `.exe` y vuelve a abrir la app.
- *"el TCP pasa pero el saludo cifrado no termina NI CON 25 SEGUNDOS"* →
  algo intercepta el TLS de salida (antivirus con inspección SSL, proxy
  o firewall de la red, filtrado del hipervisor). Subir el número no
  sirve; hay que probar en otra red o desactivar esa inspección.
- *"no hay ni TCP a ninguna dirección"* → sin internet o puerto 5432
  bloqueado. Si apagaste la red a propósito, es lo esperado.

> Ojo con `DB_CONNECT_TIMEOUT`: el límite es **por dirección IP** y el
> host de Neon resuelve a varias, así que un corte real cuesta
> (valor × direcciones) antes de pasar a modo offline. Con 3 direcciones
> y 15s son 45 segundos de ventana congelada. Entre 3 y 15 es lo sano.

### Punto 0-bis — sembrar el caché de credenciales (obligatorio)

> **Sin este paso el punto 1 falla siempre**, y falla diciendo
> "usuario o contraseña incorrectos", que despista mucho: parece que las
> credenciales están mal cuando en realidad **no hay ninguna credencial
> guardada todavía en ese equipo**.
>
> El motor offline nunca guarda contraseñas por su cuenta: solo cachea
> (como hash) la credencial de un usuario que ya se validó **con
> internet** en esa máquina. Si ese login online nunca ocurrió — y en la
> VM nunca ocurrió, porque hasta ahora la app se conectaba a un SQLite
> vacío — la caché está vacía y no hay contra qué validar sin red.

> **Los nombres de usuario distinguen mayúsculas.** En la base de
> producción los usuarios son exactamente `Ruth`, `Michelle`, `Ventas` y
> `admin` — verificado. `ventas` en minúscula o `Mich2026` **no
> existen** y el login fallará con "usuario o contraseña incorrectos"
> aunque la contraseña sea la correcta.

**Con internet todavía conectado**, y con el `.env` ya en su lugar:

- [ ] Inicia sesión con `Ruth`, y cierra sesión.
- [ ] Inicia sesión con `Michelle`, y cierra sesión.
- [ ] Inicia sesión con `Ventas`, y cierra sesión.
- [ ] En `diagnostico.log` aparece, por cada uno:
      `Credencial de '<usuario>' cacheada para poder entrar sin conexión.`

Si alguno de esos tres logins **falla estando con internet**, el problema
no es del modo offline — mándame el log y paramos ahí.

### Punto 0-ter — el aviso de CONFIGURACIÓN es distinto al de "sin conexión"

> Esta prueba es a propósito destructiva-de-mentira: se rompe el `.env`
> para ver qué hace la app, y luego se deja como estaba. **Haz una copia
> del `.env` antes de tocarlo.**

Son dos fallos que en pantalla se parecen y se atienden de forma
opuesta: "no hay internet" se arregla solo cuando vuelve la red; "no se
pudo leer la configuración" **no se arregla nunca solo** — hace falta
que alguien coloque el archivo. Si la app los muestra igual, se pierden
horas esperando a que se resuelva algo que no se va a resolver.

**Con internet conectado** (para que no se confunda con un corte real):

- [ ] Copia el `.env` a un lugar seguro (`.env.respaldo`).
- [ ] Renombra el `.env` a `.env.apagado` y abre el `.exe`.
- [ ] **La app ABRE igual** — no se queda en blanco, no se cierra sola,
      no niega la entrada. (Es a propósito: perder la base es un estado
      esperado del motor offline.)
- [ ] Arriba se ve el aviso **rojo** que dice *"No se pudo leer la
      configuración de la base de datos"* y *"esto no se arregla
      reconectándote a internet"* — **no** el aviso amarillo/azul normal
      de "sin conexión".
- [ ] El aviso trae abajo, en letra chica, `Detalle para soporte:
      DATABASE_URL no está definida`.
- [ ] En `diagnostico.log` aparece `SIN CONFIGURACIÓN DE NUBE UTILIZABLE
      — motivo: DATABASE_URL no está definida`.
- [ ] Cierra la app. Ahora edita el `.env` y deja `DATABASE_URL=basura`
      (sin comillas). Abre el `.exe` otra vez.
- [ ] **La app vuelve a abrir** (antes esto reventaba el arranque entero:
      ni ventana, ni log, ni mensaje) y el aviso ahora dice `Detalle para
      soporte: DATABASE_URL no se pudo interpretar (UnknownSchemeError)`.
- [ ] Restaura tu `.env` bueno y vuelve a abrir: el aviso rojo
      desaparece.

---

## Los puntos de prueba

### 1. Abrir el `.exe` SIN conexión y entrar con los tres roles

Desactiva el adaptador de red de la VM **antes** de abrir la app.

- [ ] El `.exe` abre (la ventana aparece; no se queda colgada ni tarda
      medio minuto).
- [ ] `Ruth` inicia sesión.
- [ ] `Michelle` inicia sesión.
- [ ] `Ventas` inicia sesión.

Si dice "usuario o contraseña incorrectos", **no asumas que la
contraseña está mal**: busca en `diagnostico.log` la línea que empieza
con `Login offline RECHAZADO` — ahí dice el motivo real, que puede ser
"no tiene credencial cacheada en este equipo" (falta el Punto 0-bis),
"la contraseña no coincide con el hash cacheado", o "está marcado como
inactivo". Son tres problemas distintos con soluciones distintas.

### 2. Cortar la red con la app ya abierta, en la pantalla de login

- [ ] Abre la app **con** internet, quédate en la pantalla de login.
- [ ] Desactiva el adaptador de red.
- [ ] Inicia sesión — debe funcionar igual, sin congelarse.
- [ ] La ventana sigue respondiendo (los botones y enlaces reaccionan).

### 3. Registrar un movimiento offline con cada rol

Con la red aún cortada:

- [ ] `Ventas`: registra una venta. La fecha queda fija en hoy, sin poder
      elegirla.
- [ ] `Michelle`: registra una entrada (o merma, o conteo físico).
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

Y con `Ventas`:
- [ ] No puede entrar a entradas, mermas, ajustes, conteo físico ni
      reportes (bloqueo por rol, no un crash).

### 6-bis. Historial NO se puede editar ni eliminar sin conexión

> Absorbe el prompt 33b. Se prueba aparte porque es el caso donde un
> fallo **corrompe datos** en vez de solo molestar: una edición aceptada
> sin conexión se escribiría contra una base que no está ahí, o peor,
> quedaría a medias.

Con la red cortada, en **Historial**, con **cada uno de los tres roles**:

- [ ] El aviso amarillo de arriba dice que *"Editar o eliminar un
      registro sigue necesitando conexión"*.
- [ ] Haz clic en **Editar** de cualquier fila → sale la pantalla
      *"Esta función requiere conexión a internet"*. **No** se abre el
      formulario, **no** sale un error técnico de Django.
- [ ] Haz clic en **Eliminar** de cualquier fila → lo mismo.
- [ ] Repite con una fila marcada como **Pendiente** (registrada offline
      hace un momento) — también debe bloquear.
- [ ] Lo mismo desde **Correcciones**.

> Si aquí algo **sí** deja editar: anótalo con el número de fila y
> mándame el log. No lo des por bueno solo porque el resto funcionó.

### 7. La ventana nunca queda insensible

> Este punto no se prueba "de paso": se prueba a propósito, porque el
> síntoma que reportaste (*la app deja de responder*) es distinto de
> *la app da error*, y solo se ve haciendo clic de verdad.

Con la red cortada, y **cronómetro en mano**:

- [ ] Haz clic en 5 secciones distintas del navbar, **una tras otra sin
      esperar** a que termine la anterior. La ventana debe seguir
      redibujándose; Windows **no** debe mostrar *"(No responde)"* en la
      barra de título.
- [ ] Deja el Inicio abierto 2 minutos sin tocar nada (el tablero
      consulta el estado de sincronización cada 4 segundos). Al volver,
      el primer clic responde de inmediato.
- [ ] Anota la espera más larga que hayas visto, en segundos: **____ s**.
      Referencia medida en Mac con la base inalcanzable de verdad (una IP
      que descarta paquetes, no un puerto que rechaza): de 57 páginas
      cargadas con los 3 roles, **56 tardaron 0.02 s o menos** y **una
      sola tardó 3.04 s** — la primera después de caerse la red, que es
      la que descubre el corte. Si en Windows ves esperas de 3 s
      repetidas, o cualquiera por encima de ~6 s, anótalo: no es lo
      esperado.
- [ ] Busca en `diagnostico.log` la línea `hay_conexion() tardó`. Si
      aparece, anota el número: significa que un sondeo pasó de 5 s, o
      sea que el `connect_timeout` no se está respetando.
- [ ] Busca las líneas `TIEMPO  XXXXms`: apunta la más alta.

---

## Qué mandarme

1. El archivo **`diagnostico.log`** completo (junto al `.exe`) — es lo más
   importante. Ahora sí registra cada request y cada sondeo de conexión.
2. Qué puntos fallaron y qué viste exactamente.
3. Si algo se congeló: cuántos segundos aproximadamente, y en qué
   pantalla.

### Cómo se ve un problema en el log

- `hay_conexion() tardó XX.XXs` → un sondeo pasó de 5 segundos, o sea
  que el `connect_timeout=3` no se está respetando (OPTIONS que no llegó
  al driver, o un DNS colgado antes de la conexión). Con un corte de red
  normal esta línea **no debe aparecer**: medido contra una IP que
  descarta paquetes, cada intento falla en 3.03 s.
- `PRUEBA DE ESCRITURA ... FALLÓ` → la app no puede escribir junto al
  `.exe` (permisos de Windows, o Control de acceso a carpetas de
  Windows Defender). Nada se guardaría.
- `SIN CONFIGURACIÓN DE NUBE UTILIZABLE — motivo: ...` → falta el
  `.env` o su `DATABASE_URL` no sirve. El motivo dice cuál de los dos.
  Volver a la Preparación. Esto **no** se arregla esperando a que vuelva
  internet.
- `Login offline RECHAZADO: ... no tiene credencial cacheada` → falta el
  Punto 0-bis: ese usuario nunca inició sesión con internet en ese equipo.
- `Login offline RECHAZADO: la contraseña ... no coincide` → la
  contraseña cambió en la nube después del último login online ahí;
  hay que entrar una vez con internet para refrescar el hash.

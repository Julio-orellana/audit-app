# Checklist de entrega — build final en Windows (prompt 37, punto 5)

Esta lista es **corta a propósito**: solo cubre lo que cambió en el
prompt 37 y lo que el prompt pide verificar en la VM. Las pruebas del
motor offline en profundidad siguen en `checklist_pruebas_manuales_windows.md`
y las del navbar en `checklist_pruebas_navbar_windows.md`.

## Antes de empezar

1. En la VM, dentro de la carpeta del proyecto:

   ```
   git pull origin v2
   build_exe.bat
   ```

   El commit que tiene que quedar es `3e178b9`. Compruébalo con
   `git log --oneline -1` antes de compilar.

2. `build_exe.bat` respalda y restaura solo el `.env`, la base local, las
   sesiones y los respaldos. Si al terminar te dice que **falta el
   `.env`**, cópialo junto al `.exe` antes de seguir: sin él la app
   arranca pero no sincroniza nada.

3. La migración `0016` ya está aplicada en Neon, así que el arranque no
   debería aplicar ninguna. Si el log dice que aplicó algo, **para y
   avísame** antes de continuar.

---

## 1. Lo más importante: el menú ya no depende de internet

Esto es lo que estaba roto y no estaba en ninguna lista previa.

1. Abre el `.exe` **con internet** e inicia sesión como Ruth.
2. **Desconecta la red por completo** (adaptador deshabilitado, no solo
   wifi apagado).
3. Recarga la pantalla y haz clic en el botón de menú (las tres rayas,
   arriba a la derecha).

   - **Tiene que abrirse** y mostrar los enlaces: Inicio, Categorías,
     Productos, Historial, Diferencias, Reportes, Sincronización,
     Correcciones, Instrucciones, más Refrescar / tu nombre / Salir.
   - Antes de este cambio, sin internet **no se abría en absoluto**. Si
     no se abre, el arreglo no llegó al build.

4. Con el menú abierto, pulsa `Escape`: debe cerrarse.
5. Navega a Historial usando el menú, sin internet. Debe abrir.

## 2. El programa se ve igual con y sin internet

Con la red desconectada, mira:

- El banner amarillo de **"Sin conexión"** en Historial: debe verse con
  su color de aviso, no como texto plano.
- La letra: debe ser la misma que con internet (Segoe UI). Ya no se
  descarga ninguna fuente.
- **Ninguna pantalla debe verse "desarmada"** respecto a como se veía
  con conexión.

## 3. La pantalla de inicio del vendedor

Con internet, entra como **Ventas**.

- El catálogo debe ocupar **todo el ancho** de la ventana, con las
  columnas Producto y Precio completas.
- Antes quedaba encogido en una columna angosta y cortada por la
  derecha. Pruébalo además con la ventana a media pantalla.

## 4. Sesión caducada: sale el login, no un error

1. Entra como Michelle y luego pulsa **Salir**.
2. Vuelve atrás en el historial del navegador o entra directo a
   Historial.

   - Debe salir el **formulario de inicio de sesión**.
   - Antes salía una página de error 403 sin ninguna salida.

## 5. Roles

Entra como **Ventas** y comprueba que **no** aparecen en el menú ni se
pueden abrir: Categorías, Productos, Historial, Diferencias, Reportes,
Correcciones. Si escribes la dirección a mano, debe salir "prohibido",
no la pantalla.

Entra como **Michelle** y comprueba que **sí** ve Historial y
Diferencias, pero **no** Correcciones (eso es solo de Ruth).

## 6. Historial: filtros y la nota nueva

Como Ruth, en Historial:

- El desplegable de **Producto** debe decir "Todos los productos" en
  español, no "- Select an option -".
- Filtra por **Tipo → "Nota automática del sistema"**. Puede que no haya
  ninguna todavía: eso es correcto y esperado. Lo que importa es que la
  opción exista en la lista y que filtrar no dé error.
- Los formularios de Registrar entrada / salida / conteo físico deben
  decir **"Selecciona un producto"**, también en español.

## 7. Movimiento offline sin duplicados

1. Sin internet, registra una venta.
2. **Cierra el programa a la fuerza** (Administrador de tareas).
3. Ábrelo de nuevo, **con internet**.
4. Espera a que sincronice y mira Historial: la venta tiene que aparecer
   **una sola vez**.

## 8. Una discrepancia real, de punta a punta

Con internet, como Ruth:

1. Registra un conteo físico de un producto con una cantidad distinta a
   la que dice el sistema.
2. Debe aparecer en el tablero, en **"Requieren atención"**, y en la
   pantalla de **Diferencias**.
3. Ábrela y resuélvela confirmando un **faltante**.
4. Comprueba en el tablero que la **ganancia neta del día BAJÓ** en
   (unidades faltantes × costo promedio). Un faltante confirmado resta;
   un sobrante no suma.

---

## Qué mandarme

- La línea de `git log --oneline -1` de la VM antes de compilar.
- Captura del menú **abierto sin internet** (punto 1) — esa es la
  importante.
- Captura de la pantalla de inicio de **Ventas** (punto 3).
- Si algo falla, el `diagnostico.log` que queda junto al `.exe`.

## Pendiente de decidir (no es una prueba)

En Neon hay **9 conteos con diferencia y ningún registro de
discrepancia**: son anteriores al prompt 34 y por eso el tablero no
avisa de ellos. Hay un comando listo y probado para registrarlos
(`manage.py registrar_discrepancias_faltantes`, con `--dry-run` para ver
qué haría sin escribir). **No se ha ejecutado.** Tiene sentido decidirlo
después de la limpieza de datos del prompt 31, que podría llevarse esos
mismos conteos por delante.

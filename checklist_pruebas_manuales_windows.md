# Checklist de pruebas manuales — .exe empaquetado en Windows

Esta lista es para que tú la corras directamente en la VM de Windows, no para Claude Code. Ve marcando cada punto; si algo falla, anota exactamente qué pasó (mensaje, pantalla, comportamiento) para poder armar un prompt correctivo puntual después.

## 1. Instalación y arranque básico

- [ ] Copia la carpeta completa del build a una ubicación normal de Windows (ej. `Escritorio` o `Documentos`), no la corras desde una carpeta temporal o de descarga comprimida sin extraer.
- [ ] Doble clic en el `.exe` — abre sin mostrar consola/terminal.
- [ ] El ícono se ve correctamente (no el genérico de Windows).
- [ ] La ventana carga el login sin errores visibles.

## 2. Login — los tres roles, con conexión

- [ ] `Ruth` inicia sesión con conexión normal.
- [ ] `Michelle`/`Mich2026` inicia sesión con conexión normal.
- [ ] `Ventas`/`ventas` inicia sesión con conexión normal.

## 3. Login offline (el punto que más falló antes)

- [ ] Corta la conexión a internet de la VM (desactiva el adaptador de red o desconecta el Wi-Fi/Ethernet).
- [ ] Cierra la app por completo si estaba abierta.
- [ ] Ábrela de nuevo sin conexión.
- [ ] `Ruth` puede iniciar sesión offline usando el caché local.
- [ ] `Michelle` puede iniciar sesión offline usando el caché local.
- [ ] `Ventas` puede iniciar sesión offline usando el caché local.

## 4. Operación offline por rol

Con la red aún cortada:

- [ ] `Ventas`: puede registrar una venta; la fecha queda forzada a hoy sin poder elegirla.
- [ ] `Ventas`: no puede acceder a entradas, mermas, ajustes, conteo físico ni reportes (debe bloquear con mensaje claro, no crashear).
- [ ] `Ruth`/`Michelle`: pueden registrar entrada, merma, ajuste y conteo físico offline.
- [ ] `Ruth`/`Michelle`: al intentar editar/eliminar un registro de Historial offline, se bloquea con el mensaje de "requiere conexión", no crashea.
- [ ] Historial (lectura) se puede consultar offline y muestra los movimientos combinados (locales + ya sincronizados), marcando de alguna forma cuáles son locales/pendientes.
- [ ] Navega por cada sección del navbar una por una, offline, con cada rol — ninguna debe crashear con un error técnico. Las que no deban funcionar offline (reportes, gestión de productos/usuarios) deben mostrar el mensaje claro.
- [ ] El indicador de "N movimientos pendientes de sincronizar" aparece visible.
- [ ] El botón de refrescar funciona estando offline (recarga la vista actual sin errores).

## 5. Persistencia ante cierre forzado (los tres roles)

Con la red aún cortada:

- [ ] Con `Ventas`, registra un movimiento pendiente, fuerza el cierre del proceso desde el Administrador de tareas (no lo cierres normal), reábrelo — el pendiente sigue ahí.
- [ ] Repite lo mismo con `Ruth` y con `Michelle`.

## 6. Sincronización real al reconectar

- [ ] Con los pendientes generados en el punto anterior, reconecta la red.
- [ ] Sin tocar nada manualmente, espera un momento y confirma que la sincronización ocurre sola.
- [ ] Verifica en Neon (o pídeme ayuda para consultarlo) que esos movimientos aparecen, sin duplicados.
- [ ] El stock/inventario refleja correctamente esos movimientos después de sincronizar.
- [ ] Al reabrir la app con conexión después de haber tenido pendientes, aparece el indicador de "subiendo cambios pendientes" y el dashboard se refresca solo al terminar.

## 7. Cola de sincronización (pantalla nueva)

- [ ] Accesible para `Ruth`/`Michelle`, lista los pendientes con tipo, producto, cantidad y fecha.
- [ ] El botón de "reintentar ahora" funciona si fuerzas un fallo (por ejemplo cortando la red justo antes de reintentar).

## 8. Validación de stock

- [ ] Intenta vender más unidades de las que hay disponibles de un producto normal — debe rechazarse con el mensaje de inventario insuficiente.
- [ ] Repite con un producto derivado (ej. un "Cubetazo") — debe considerar el equivalente correcto según su `factor_equivalencia`.
- [ ] Prueba esto tanto online como offline.

## 9. Cierre limpio del proceso

- [ ] Cierra la app normalmente (no forzado) y confirma en el Administrador de tareas que el proceso de Python/waitress termina por completo, sin quedar corriendo en segundo plano.
- [ ] Abre el `.exe` una segunda vez inmediatamente después — no debe haber conflicto de puerto ni error de "ya en uso".

## 10. Prueba de portabilidad (mover la carpeta)

- [ ] Con la app ya usada (con caché de credenciales y algún pendiente generado), copia la carpeta completa del build a una tercera ubicación distinta en la misma VM.
- [ ] Ábrela desde ahí y confirma que reconoce el caché/pendientes existentes, no que se comporta como una instalación nueva desde cero.

---

Si todos estos puntos pasan bien, la app está lista para el prompt 31 (limpieza final) cuando se reinicie tu cuota semanal. Si algo falla, anótalo con el detalle exacto (rol, paso, mensaje de error o comportamiento) para armar un prompt correctivo dirigido a ese punto específico.

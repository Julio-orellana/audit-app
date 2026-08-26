# Diagnóstico general — funcionalidad y desempeño

Fecha: 2026-08-26. Entorno: desarrollo (Mac), app corriendo contra la base real en Neon (Postgres), tal como queda configurada desde el prompt 18. Todas las mediciones y pruebas de este documento se hicieron con el cliente de pruebas de Django (`django.test.Client`) contra la base de datos real — no es una estimación. Cada prueba de escritura se limpió inmediatamente después (el catálogo, los movimientos reales y los usuarios quedaron exactamente igual que antes de empezar este diagnóstico, verificado al final).

**Este documento es solo diagnóstico — no se corrigió nada de lo que se encontró.**

---

## 1. Barrido funcional por rol

### Acceso a pantallas (lectura)

| Rol | Pantalla | Resultado |
|---|---|---|
| admin (Ruth) | Inicio, Categorías, Productos, Historial, Reportes, Correcciones, Instrucciones, `/admin/` (Django admin) | ✅ Funciona — las 8 dan 200 |
| auditor (Mich2026) | Inicio, Categorías, Productos, Historial, Reportes, Instrucciones | ✅ Funciona — las 6 dan 200 |
| vendedor (usuario de prueba temporal, creado y eliminado para este diagnóstico) | Inicio (reducido), Instrucciones | ✅ Funciona — las 2 dan 200 |

### Bloqueo real a nivel de vista (URL directa, no solo el menú)

| Rol | Intento | Resultado |
|---|---|---|
| auditor | GET `/correcciones/`, `/lotes/<id>/editar/`, `/lotes/<id>/eliminar/`, `/salidas/<id>/editar/`, `/salidas/<id>/eliminar/`, `/conteos/<id>/editar/`, `/conteos/<id>/eliminar/` | ✅ Las 7 dan 403 |
| vendedor | GET `/reportes/`, `/historial/`, `/categorias/`, `/productos/`, `/lotes/nuevo/`, `/conteos/nuevo/`, `/correcciones/`, más las 3 URLs de edición de historial | ✅ Las 10 dan 403 |

Ningún camino directo por URL le da acceso real a algo fuera de su rol — el sistema de permisos del prompt 16/17 sigue sólido.

### Operaciones de escritura

| Operación | Rol probado | Resultado | Detalle |
|---|---|---|---|
| Registrar entrada (LoteCompra) | admin | ✅ Funciona | Guardado y recuperado correctamente |
| Registrar venta | admin | ✅ Funciona | — |
| Registrar merma | admin | ✅ Funciona (con motivo) | Sin motivo, se rechaza correctamente (ver §4) |
| Registrar ajuste manual | admin | ✅ Funciona (con motivo) | — |
| Venta de un producto **derivado** (`producto_base`) | admin | ✅ Funciona | Vendí 2 unidades de "Sprite colaboradores" (factor 1) → el stock de "Sprite" bajó exactamente 2. Confirmado con números reales, no solo status 200. |
| Conteo físico con diferencia | admin | ✅ Funciona | Diferencia calculada correctamente contra el stock *a la fecha del conteo* (nota metodológica: mi primer intento de probar esto dio una "falla" falsa porque comparé contra el stock sin filtrar por fecha en vez del stock a la fecha del conteo — al corregir el cálculo de referencia, todo cuadra exactamente) |
| Generar ajuste desde un conteo | admin | ✅ Funciona (en el caso normal) | Stock queda exactamente en la cantidad contada — **pero ver el hallazgo crítico de concurrencia en §3/§4: dos clics casi simultáneos SÍ duplican el ajuste** |
| Editar un registro de historial (LoteCompra) | admin | ✅ Funciona | Motivo vacío → rechazado (200, sin guardar). Con motivo → guardado y crea `CorreccionHistorial` con `datos_anteriores`/`datos_nuevos` correctos |
| Eliminar un registro de historial | admin | ✅ Funciona | Motivo vacío → rechazado. Con motivo → eliminado y `CorreccionHistorial` con `accion="eliminacion"` creado |
| Cualquier registro/edición de historial | auditor, vendedor | ✅ Bloqueado (403) | Confirmado por URL directa, no solo por ausencia de botón |

**Conclusión de la parte 1: no encontré ninguna funcionalidad que esté rota hoy.** Todo lo que debería funcionar, funciona; todo lo que debería estar bloqueado, está bloqueado de verdad. Los hallazgos de este diagnóstico están en el desempeño (§2/§3) y en comportamientos frágiles no disparados todavía (§4).

---

## 2. Tiempos de respuesta (promedio de 3 corridas, contra Neon)

| Operación | Tiempos individuales (s) | Promedio | Consultas SQL |
|---|---|---|---|
| Login | 1.22 / 1.22 / 1.18 | **1.21 s** | 9 (fijo) |
| Dashboard admin (Ruth) | 36.90 / 35.29 / 36.67 | **36.29 s** | 401 (fijo, no varía entre corridas) |
| Dashboard auditor (Mich2026) | 36.39 / 36.25 / 36.62 | **36.42 s** | 403 |
| Dashboard vendedor | 0.59 / 0.60 / 0.60 | **0.60 s** | 7 |
| Historial sin filtros | 3.19 / 3.19 / 3.32 | **3.24 s** | 33 |
| Historial filtrado por producto | 3.14 / 2.84 / 2.84 | **2.94 s** | 29 |
| Historial filtrado por fecha | 3.15 / 3.16 / 3.25 | **3.19 s** | 33 |
| Reporte en pantalla (mes, todos los productos) | 20.49 / 19.85 / 18.95 | **19.76 s** | 219 |
| Reporte Excel (mes, todos los productos) | 20.96 / 21.45 / 20.97 | **21.13 s** | 236–243 |
| Guardar LoteCompra | 0.655 / 0.503 / 0.503 | **0.554 s** | 6 (fijo) |
| Guardar MovimientoSalida (venta) | 0.838 / 0.770 / 0.781 | **0.796 s** | 9 (fijo) |
| Guardar MovimientoSalida (merma) | 0.832 / 0.764 / 0.772 | **0.789 s** | 9 (fijo) |
| Guardar MovimientoSalida (ajuste) | 0.882 / 0.778 / 0.806 | **0.822 s** | 9 (fijo) |
| Guardar ConteoFisico | 0.593 / 0.637 / 0.589 | **0.606 s** | 6 (fijo) |

No hubo variación relevante entre las 3 corridas de ninguna medición (nada de "arranque en frío" aislado — la sesión ya llevaba varios minutos de uso continuo cuando se midió, así que estos números reflejan uso normal, no el peor caso de una conexión recién despertada). Cada medición de guardar un movimiento limpió el registro creado inmediatamente después de medirlo, así que las 3 repeticiones de cada una partieron siempre del mismo estado — confirmado al final que LoteCompra/MovimientoSalida/ConteoFisico quedaron con la misma cantidad de filas que tenían antes de este diagnóstico.

**Lectura directa:** las operaciones de guardar un solo movimiento (la acción más frecuente en el uso real) están bien — bajo 1 segundo. Los problemas serios son las tres vistas que **recorren muchos registros en un bucle**: Dashboard, Reportes y, en menor medida hoy, Historial.

---

## 3. Conteo de consultas SQL — patrón N+1 más allá del dashboard

El dashboard (401 consultas) ya se sabía. Encontré el **mismo patrón en dos vistas más**, con la causa exacta identificada:

| Vista | Consultas | Escala con... | Causa (archivo:función) |
|---|---|---|---|
| Dashboard (admin/auditor) | 401–403 | Cantidad de productos activos (26) | `home()` en `views.py` — llama `producto.stock_teorico()` y `producto.costo_promedio()` por cada producto en un bucle Python; cada llamada dispara sus propias consultas (y para productos base, un bucle interno extra sobre `self.derivados.all()`) |
| **Reportes** (pantalla y Excel) | 219 (1 producto: 16 → 26 productos: 219) | Cantidad de productos incluidos — confirmado midiendo con 1 producto vs 26: **~8.1 consultas extra por producto** | `resumen_general()` en `services.py` — llama `resumen_producto()` por cada producto, que hace 3 `.aggregate()` propios más `producto.stock_teorico()` (que a su vez puede iterar derivados) |
| **Historial** | 33 con los 5 conteos actuales, 8 con un rango sin conteos — confirmado midiendo con 0 conteos vs 5: **~5 consultas extra por conteo físico** | Cantidad de `ConteoFisico` en el rango mostrado (no la cantidad de LoteCompra/MovimientoSalida, esos no agregan consultas extra) | `movimientos_periodo()` en `services.py` — arma el `detalle` de cada fila de conteo accediendo a `conteo.diferencia`, una property de `ConteoFisico` que llama `stock_teorico(hasta_fecha=...)` por cada conteo |

**Por qué importa que Historial "solo" tenga 33 hoy:** ya está claro que el número crece 1:1 con la cantidad de conteos físicos guardados. Con el uso recomendado (un conteo semanal por producto, prompt 8), en un año esto son decenas de conteos por producto — esta vista va a degradarse con el tiempo exactamente como el dashboard, solo que más lento porque hoy hay pocos conteos.

**Vistas que SÍ están bien (consultas fijas, no crecen):**
- Login: 9 consultas fijas.
- Dashboard del vendedor: 7 consultas fijas — porque su plantilla no itera productos calculando stock/costo por cada uno, solo lista nombre+precio ya traídos con una sola consulta agrupada por categoría (`productos_activos_por_categoria()`).
- Guardar un movimiento individual (LoteCompra/MovimientoSalida/ConteoFisico): 7–10 consultas fijas, no importa cuántos productos o movimientos existan ya.
- Correcciones al historial (lista): consulta fija (no lo medí formalmente en este pase, pero no tiene ningún bucle por producto en su código).

---

## 4. Qué podría fallar (no solo lo que ya falla)

Ordenado por severidad.

### 🔴 Crítico

**4.1 — Doble clic (o doble request casi simultáneo) en "Generar ajuste" duplica el ajuste de stock.**
Confirmado en vivo: dos POST casi simultáneos al mismo `/conteos/<id>/generar-ajuste/` **ambos** pasaron la validación `if conteo.ajuste_generado_id: ...` (ambos la leyeron como `None` antes de que cualquiera de los dos terminara de guardar) y **ambos crearon un `MovimientoSalida` de ajuste**. El conteo terminó apuntando a uno solo de los dos (`ajuste_generado`), pero el otro quedó huérfano y sigue afectando el stock igual — el ajuste se aplicó dos veces sobre el inventario real.
- **Causa:** `generar_ajuste()` en `views.py` es un patrón clásico de "leer, decidir, escribir" (TOCTOU) sin ningún bloqueo (`select_for_update()`) ni restricción a nivel de base de datos que impida crear un segundo ajuste para el mismo conteo.
- **Cuándo se dispara en la práctica:** un doble clic accidental en el botón (no hay protección de doble-submit en ningún formulario de esta app, ver 4.6), o dos personas con la sesión de admin abierta revisando la misma alerta al mismo tiempo.
- **Impacto:** corrompe silenciosamente el stock teórico real — exactamente el tipo de error que esta app existe para prevenir.

**4.2 — Reportes (pantalla y Excel) tarda ~20 segundos y dispara ~220–240 consultas — mismo patrón N+1 que el dashboard, no solo el dashboard.**
Ya cubierto en detalle en §3. Lo marco crítico porque Reportes es, junto con el dashboard, de las pantallas de más uso — y las 219 consultas concentradas en ~20 segundos son también 219 oportunidades de que una de ellas falle si la conexión a Neon se cae a medio camino (ver 4.3).

### 🟠 Importante

**4.3 — No hay manejo de errores de red a media operación, más allá de lo que ya cubre `CONN_HEALTH_CHECKS` (prompt 18).**
Revisé `views.py`, `services.py`, `reportes.py` y `models.py` completos: no hay un solo `try/except` alrededor de una operación de base de datos en toda la app. `CONN_HEALTH_CHECKS=True` (prompt 18) solo resuelve el caso de una conexión que ya estaba muerta *antes* de empezar una request nueva — la reconecta automáticamente. No hace nada si la conexión se cae *durante* una request que ya está en curso: eso simplemente revienta como un 500 sin manejar. Esto es un riesgo teórico, no algo que haya logrado reproducir (no intenté cortar la red a mitad de una consulta), pero el riesgo es directamente proporcional a cuántas consultas dispara cada vista — y por eso conecta con 4.2: Reportes, con sus ~220 consultas en 20 segundos, es hoy la vista con más ventana de exposición a este problema, seguida del Dashboard con sus 401.
- **Qué pasaría en la práctica:** el usuario ve una página de error de Django (técnica, porque `DEBUG=True` sigue activo — ver 4.7) en vez de un mensaje claro, sin ninguna indicación de si algo se guardó a medias. Para las vistas de solo lectura (Reportes, Historial, Dashboard) esto no corrompe datos, solo es una mala experiencia. Para una vista de escritura no hay ninguna todavía con tantas consultas como para que esto sea un riesgo real hoy — pero seguirá empeorando junto con 4.2/4.4 si crecen.

**4.4 — Un producto "desactivado" sigue apareciendo como opción válida en Registrar entrada, Registrar salida y Conteo físico.**
Confirmado en vivo: desactivé un producto de prueba (y lo reactivé después) y verifiqué que `LoteCompraForm`, `MovimientoSalidaForm` y `ConteoFisicoForm` — a diferencia de `ReporteForm`, que sí filtra `activo=True` — usan `Producto.objects.all()` por default para el campo `producto` (nunca se restringió explícitamente). El manual de usuario (`instrucciones.html`, prompt 13a) dice textualmente que desactivar un producto hace que "deje de aparecer... en los formularios nuevos" — eso no es cierto para estos tres formularios.
- **Impacto:** un auditor puede seguir registrando compras/ventas/conteos contra un producto que se suponía retirado del menú, sin ningún aviso.
- **Causa:** falta un `queryset=Producto.objects.filter(activo=True)` (con la excepción de permitir seguir viendo el histórico ya guardado, que no se toca) en esos tres formularios.

### 🟡 Menor

**4.5 — Historial no valida `fecha_desde > fecha_hasta`; Reportes sí, para el mismo tipo de error de usuario.**
Confirmado: `ReporteForm.clean()` sí compara `fecha_inicio`/`fecha_fin` y muestra "La fecha final no puede ser anterior a la fecha inicial." `HistorialFiltroForm` no tiene ningún `clean()` — con fechas invertidas, la consulta simplemente no encuentra nada y la pantalla dice "Sin movimientos con estos filtros", sin explicar que el rango está invertido. No es peligroso (no hay manera de que esto corrompa datos), solo inconsistente y un poco confuso para quien lo teclee mal.

**4.6 — Ningún formulario de creación tiene protección contra doble-submit.**
Revisé todos los templates: no hay JavaScript en esta app (confirmé que no existe ni un solo `<script>` propio fuera del bundle de Bootstrap, ni `disabled`/debounce en ningún botón de guardar). Un doble clic en "Guardar" en Registrar entrada/salida/Conteo físico podría crear dos filas idénticas — no lo reproduje deliberadamente para LoteCompra/MovimientoSalida/ConteoFisico (son creates simples sin la complejidad de "Generar ajuste"), pero el mecanismo es el mismo que confirmé en 4.1: nada en el backend impide dos POST idénticos seguidos.

**4.7 — `DEBUG=True` sigue activo.** Ya lo había señalado como nota de bajo riesgo en el prompt 13b (relevante ahí para el empaquetado). Lo repito aquí porque cambia de contexto: ahora la app está conectada a una base de datos real en la nube, así que una página de error técnica (por ejemplo, disparada por 4.1 o 4.3) mostraría más contexto interno del que mostraría en producción — aunque Django ya censura automáticamente el valor de `PASSWORD` dentro de `DATABASES` en esas páginas.

**4.8 — Selección de un producto inactivo en Reportes se descarta en silencio, sin avisar.**
Confirmado: al mandar por URL directa el id de un producto inactivo en `?productos=`, `ReporteForm` lo rechaza como "no es una opción válida" — pero como eso invalida el formulario completo, `_resolver_productos()` cae a su comportamiento por default (None = todos los productos activos) sin decirle al usuario que su selección específica fue ignorada. No es alcanzable desde la UI normal (el checkbox de un producto inactivo ni se dibuja), solo por URL manipulada a mano — impacto mínimo, pero vale la pena saber que existe.

**4.9 — Concurrencia en creación simple de movimientos: no encontré ningún riesgo, y hay una razón de diseño.**
Esto es una nota positiva, no un hallazgo de falla: el stock nunca se guarda como un número cacheado (no hay una columna `stock` en `Producto` que alguien tenga que leer-modificar-escribir). `stock_teorico()` siempre sale de sumar `LoteCompra`/`MovimientoSalida` desde cero en cada llamada. Eso significa que dos usuarios registrando ventas del mismo producto casi al mismo tiempo son, a nivel de base de datos, dos `INSERT` independientes — no hay ningún "último en escribir gana" que pierda el movimiento de alguno de los dos. El único punto donde sí aparece un problema de concurrencia real es 4.1 (`generar_ajuste`), precisamente porque ahí sí hay un patrón leer-decidir-escribir sobre `ConteoFisico.ajuste_generado`.

**4.10 — `manage.py test` no corre limpio contra Neon (ya reportado en el prompt 18, lo repito por completitud).**
Falla al intentar borrar la base de prueba (`database "test_neondb" is being accessed by other users"`) por el pooling de conexiones. Los tests sí pasan corriéndolos contra SQLite (moviendo el `.env` temporalmente). No es un problema de la app en sí, es fricción de herramientas.

---

## Resumen para decidir prioridades

| # | Hallazgo | Severidad | Ya sabíamos |
|---|---|---|---|
| 4.1 | Doble ajuste por race condition en generar-ajuste | 🔴 Crítico | No — nuevo |
| Dashboard N+1 (401 consultas, 36s) | 🔴 Crítico | Sí — prompt 18b lo va a corregir |
| 4.2 | Reportes N+1 (219–243 consultas, ~20s) | 🔴 Crítico | No — nuevo |
| 4.3 | Sin manejo de fallas de red a media request | 🟠 Importante | Parcial (se sabía de CONN_HEALTH_CHECKS, no de su límite) |
| Historial N+1 (33 consultas hoy, crece con los conteos) | 🟠 Importante | No — nuevo |
| 4.4 | Producto inactivo seleccionable en formularios de escritura | 🟠 Importante | No — nuevo |
| 4.5 | Historial no valida fechas invertidas | 🟡 Menor | No — nuevo |
| 4.6 | Sin protección de doble-submit en ningún formulario | 🟡 Menor | No — nuevo |
| 4.7 | DEBUG=True con base de datos real conectada | 🟡 Menor | Sí (prompt 13b) |
| 4.8 | Producto inactivo en Reportes se descarta en silencio | 🟡 Menor | No — nuevo |
| 4.10 | `manage.py test` no corre limpio contra Neon | 🟡 Menor | Sí (prompt 18) |

Ningún dato real se perdió ni se modificó durante este diagnóstico — todas las pruebas de escritura (incluida la que confirmó la duplicación de ajustes) se hicieron sobre registros de prueba y se limpiaron de inmediato; el catálogo, los movimientos reales y los usuarios quedaron verificados exactamente igual que antes de empezar.

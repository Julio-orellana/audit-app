# Checklist de pruebas manuales en Windows — navbar responsivo (prompt 36)

Independiente del motor offline (ver `checklist_pruebas_manuales_windows.md`
para eso) — esto es solo interfaz. **Con internet conectado**, no hace
falta repetir nada del modo offline para este checklist.

---

## Qué se arregló (para entender qué estás confirmando)

En Mac el navbar se veía bien porque la ventana solía abrirse ancha. En
la VM de Windows, con una resolución más chica, los enlaces del navbar
(Inicio, Categorías, Productos... hasta 8 con el rol admin) no cabían en
una sola fila, pasaban a una segunda línea, y esa segunda línea se
dibujaba **encimada** sobre el contenido de la página en vez de
empujarlo hacia abajo — el navbar tenía una altura fija que no se
ajustaba.

Ahora, por debajo de cierto ancho, los enlaces y los botones de la
derecha (Refrescar, pendientes, usuario, Salir) se esconden detrás de un
botón de menú (☰, arriba a la derecha). Al hacer clic, se abre un panel
que **empuja la página hacia abajo**, nunca se dibuja encima de nada.

- [ ] `git pull` y `build_exe.bat` antes de empezar — es un cambio de
      código, no algo que se vea sin recompilar.

---

## Los 6 casos (3 resoluciones × 2 escalados)

Cambia la resolución desde la configuración de pantalla de Windows
(clic derecho en el escritorio → Configuración de pantalla) — no hace
falta cambiar el monitor físico, Windows lo simula igual.

Para cada combinación: abre el `.exe`, inicia sesión con **Ruth**
(admin, es la que ve los 8 enlaces — el caso más exigente), y en el
tablero de Inicio toma la captura.

| # | Resolución | Escalado | Capturaste |
|---|---|---|---|
| 1 | 1920×1080 (o la más grande que tengas) | 100% | [ ] |
| 2 | 1920×1080 (o la más grande que tengas) | 125% | [ ] |
| 3 | 1440×900 | 100% | [ ] |
| 4 | 1440×900 | 125% | [ ] |
| 5 | 1366×768 | 100% | [ ] |
| 6 | 1366×768 | 125% | [ ] |

En **cada** captura, confirma:

- [ ] La marca "Auditoría ¡Ay Lupita!" se ve completa a la izquierda,
      sin cortarse.
- [ ] O bien ves los 8 enlaces en una sola fila (pantallas grandes), o
      bien ves un botón ☰ solo, sin ningún enlace suelto ni texto
      encimado (pantallas chicas o con escalado).
- [ ] Nada se superpone: ni el navbar sobre el contenido, ni un enlace
      sobre otro.
- [ ] Si ves el botón ☰: haz clic. El panel debe abrir **debajo** del
      navbar, con los 8 enlaces en una lista y, más abajo, Refrescar /
      pendientes / tu usuario / Salir. El contenido del tablero debe
      quedar empujado hacia abajo, nunca tapado.
- [ ] Cierra el panel (clic en ☰ otra vez, ahora una ✕) y confirma que
      vuelve a verse como al abrir la app.

> Si en algún caso ves texto cortado, un enlace encimado con otro, o el
> panel abierto tapando el tablero en vez de empujarlo — es justo lo que
> se suponía que estaba arreglado. Anota la resolución y el escalado
> exactos, y mándame la captura.

---

## Redimensionar la ventana en vivo

El `.exe` abre en una ventana normal de Windows (se puede agrandar,
achicar y maximizar con el mouse, como cualquier otra).

- [ ] Con la app abierta y **el panel del menú abierto** (clic en ☰),
      agranda y achica la ventana arrastrando una esquina. El panel
      debe seguir viéndose bien en todo momento — nunca debe quedar
      cortado ni superpuesto mientras arrastras.
- [ ] Agranda la ventana hasta que el botón ☰ desaparezca y vuelvas a
      ver los 8 enlaces en una fila. Sigue arrastrando para achicarla de
      nuevo: el botón ☰ debe reaparecer solo, sin recargar la página.
- [ ] Maximiza la ventana (doble clic en la barra de título, o el botón
      de maximizar) y confirma que se ve bien también a pantalla
      completa.

---

## Qué mandarme

1. Las 6 capturas de la tabla de arriba (o las que hayas alcanzado a
   tomar), con la resolución y el escalado de cada una.
2. Si algo falló en la prueba de redimensionar: en qué momento exacto
   (agrandando, achicando, con el panel abierto o cerrado).
3. Cualquier otra pantalla donde notes textos encimados o cortados,
   aunque no sea el navbar — dime en cuál y a qué resolución.

### Ya verificado en Mac, para que sepas qué esperar

En el navegador, simulando estas mismas resoluciones y los anchos
equivalentes al escalado de Windows (1366×768 al 125% se comporta como
~1093px de ancho real; al 150%, ~911px):

- 1920×1080: una sola fila, idéntica a como se veía antes de este
  arreglo — sin cambios para quien ya lo veía bien.
- 1440×900 y 1366×768: botón ☰ limpio, sin traslapes.
- Los anchos equivalentes a 125%/150%: mismo resultado, incluso con la
  marca envuelta a dos líneas en el caso más angosto.
- Panel abierto + redimensionar en vivo (sin recargar): reacciona al
  instante en las dos direcciones, sin dejar nada a medio abrir.
- Confirmado con la geometría exacta de la página (no solo a ojo): el
  borde inferior del navbar coincide pixel por pixel con donde arranca
  el contenido de la página — cero superposición.
- Rol vendedor (menos enlaces) y la pantalla de inicio de sesión (sin
  usuario todavía): también correctos.

Windows con WebView2 (el motor que usa pywebview ahí) traduce el
escalado de la pantalla de forma distinta a como Mac traduce Retina, así
que aunque esto ya se ve bien en la simulación, tu confirmación real en
la VM sigue siendo la que cierra este punto.

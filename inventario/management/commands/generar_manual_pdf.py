# inventario/management/commands/generar_manual_pdf.py
"""
Genera MANUAL_USUARIO.pdf en la raíz del proyecto: el manual de usuario
completo (más detallado que la sección "Instrucciones" dentro de la app),
usando reportlab.

Importante: este manual NUNCA debe incluir usuarios ni contraseñas reales.
Donde hace falta un ejemplo de inicio de sesión, se usan credenciales
genéricas inventadas ("usuario" / "contraseña"), dejando claro que las
reales las entrega el administrador del sistema por separado.
"""
import datetime

from django.conf import settings
from django.core.management.base import BaseCommand
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#232733")
ACCENT = colors.HexColor("#b8551f")
INK_SOFT = colors.HexColor("#5a5347")
NOTE_BG = colors.HexColor("#f2ede6")
VERSION = "1.0"


def _construir_estilos():
    estilos = getSampleStyleSheet()
    estilos.add(ParagraphStyle(
        name="Portada", fontSize=30, leading=36, alignment=TA_CENTER,
        textColor=NAVY, fontName="Helvetica-Bold", spaceAfter=8,
    ))
    estilos.add(ParagraphStyle(
        name="PortadaSub", fontSize=16, leading=20, alignment=TA_CENTER,
        textColor=ACCENT, fontName="Helvetica-Bold", spaceAfter=6,
    ))
    estilos.add(ParagraphStyle(
        name="PortadaMeta", fontSize=11, leading=16, alignment=TA_CENTER,
        textColor=INK_SOFT, fontName="Helvetica",
    ))
    estilos.add(ParagraphStyle(
        name="PortadaCredito", fontSize=9, leading=13, alignment=TA_CENTER,
        textColor=INK_SOFT, fontName="Helvetica-Oblique",
    ))
    estilos.add(ParagraphStyle(
        name="Seccion", fontSize=18, leading=22, textColor=NAVY,
        fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=12,
    ))
    estilos.add(ParagraphStyle(
        name="SubSeccion", fontSize=13, leading=17, textColor=ACCENT,
        fontName="Helvetica-Bold", spaceBefore=14, spaceAfter=6,
    ))
    estilos.add(ParagraphStyle(
        name="Cuerpo", fontSize=10.5, leading=15, spaceAfter=8,
        fontName="Helvetica", textColor=colors.HexColor("#232733"),
    ))
    estilos.add(ParagraphStyle(
        name="CuerpoNota", fontSize=10, leading=14, spaceAfter=8,
        fontName="Helvetica-Oblique", textColor=INK_SOFT,
    ))
    estilos.add(ParagraphStyle(
        name="Codigo", fontSize=9.5, leading=13, spaceAfter=8,
        fontName="Courier", textColor=NAVY, backColor=colors.HexColor("#f2ede6"),
        borderPadding=6,
    ))
    return estilos


def _nota(texto, estilos):
    """Caja de nota destacada (fondo tostado), para advertencias y consejos."""
    tabla = Table([[Paragraph(texto, estilos["CuerpoNota"])]], colWidths=[16 * cm])
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#ddd2c2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return tabla


def _lista(items, estilos):
    return ListFlowable(
        [ListItem(Paragraph(item, estilos["Cuerpo"]), leftIndent=6) for item in items],
        bulletType="bullet", start="•", leftIndent=16,
    )


def _lista_numerada(items, estilos):
    return ListFlowable(
        [ListItem(Paragraph(item, estilos["Cuerpo"]), leftIndent=6) for item in items],
        bulletType="1", leftIndent=18,
    )


def _construir_contenido(estilos):
    story = []

    # --- 1. Portada ---------------------------------------------------
    story.append(Spacer(1, 5 * cm))
    story.append(Paragraph("Manual de Usuario", estilos["Portada"]))
    story.append(Paragraph("Auditoría de Inventario — ¡Ay Lupita!", estilos["PortadaSub"]))
    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        f"Versión {VERSION} · {datetime.date.today().strftime('%d/%m/%Y')}",
        estilos["PortadaMeta"],
    ))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "Para la persona encargada de la auditoría de inventario de bebidas embotelladas.",
        estilos["PortadaMeta"],
    ))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "Sistema local e independiente del sistema principal (POS) del restaurante — "
        "no requiere internet para funcionar.",
        estilos["PortadaMeta"],
    ))
    story.append(Spacer(1, 2.5 * cm))
    story.append(Paragraph(
        "Desarrollado por Julio Orellana · julioes134@outlook.es",
        estilos["PortadaCredito"],
    ))
    story.append(PageBreak())

    # --- 2. Instalación (primera vez) ----------------------------------
    story.append(Paragraph("1. Instalación (primera vez)", estilos["Seccion"]))
    story.append(Paragraph(
        "Esta sección la realiza, normalmente una sola vez, la persona que prepara el equipo "
        "(puede ser el administrador del sistema, no necesariamente el auditor).",
        estilos["Cuerpo"],
    ))

    story.append(Paragraph("Versión entregada como aplicación (la forma normal de instalarlo)", estilos["SubSeccion"]))
    story.append(_lista_numerada([
        "Copia la carpeta completa <font face=\"Courier\">AuditoriaAylupita</font> (contiene "
        "<font face=\"Courier\">AuditoriaAylupita.exe</font> y varios archivos de soporte a su "
        "alrededor) al equipo del auditor, en cualquier ubicación — por ejemplo el Escritorio o "
        "\"Documentos\".",
        "Opcional: crea un acceso directo a <font face=\"Courier\">AuditoriaAylupita.exe</font> en "
        "el Escritorio, para no tener que entrar a la carpeta cada vez.",
    ], estilos))
    story.append(_nota(
        "No se necesita instalar Python, ni nada más — el ejecutable ya incluye todo lo necesario "
        "para funcionar. Es importante copiar la carpeta <b>completa</b>, no solo el archivo "
        "<font face=\"Courier\">.exe</font> suelto: los archivos de alrededor son parte del "
        "programa, aunque al usuario final solo le importe el acceso directo al <font "
        "face=\"Courier\">.exe</font>.",
        estilos,
    ))

    story.append(Paragraph("Versión de código fuente (alternativa, para quien prepara o modifica el sistema)", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Si en cambio recibiste el código fuente sin compilar (por ejemplo, para desarrollo o "
        "para generar tú mismo el ejecutable), hace falta Python instalado en el equipo:",
        estilos["Cuerpo"],
    ))
    story.append(_lista_numerada([
        "Copia la carpeta completa del proyecto al equipo, en cualquier ubicación.",
        "Abre una terminal (Símbolo del sistema) dentro de esa carpeta.",
        "Crea el entorno virtual: <font face=\"Courier\">python -m venv venv</font>",
        "Actívalo: <font face=\"Courier\">venv\\Scripts\\activate</font>",
        "Instala las dependencias: <font face=\"Courier\">pip install -r requirements.txt</font>",
        "Aplica las migraciones de la base de datos: "
        "<font face=\"Courier\">python manage.py migrate</font>",
    ], estilos))

    # --- 3. Cómo se inicia el programa ----------------------------------
    story.append(Paragraph("2. Cómo se inicia el programa", estilos["Seccion"]))
    story.append(_lista_numerada([
        "Haz doble clic en <font face=\"Courier\">AuditoriaAylupita.exe</font> (o en su acceso "
        "directo, si dejaste uno en el Escritorio).",
        "Se abre directamente la ventana propia del programa, titulada \"Auditoría Aylupita\" — "
        "no aparece ninguna ventana negra de terminal de por medio.",
        "Inicia sesión con tu usuario y contraseña (te los entrega el administrador del sistema "
        "por separado — nunca están escritos en este manual).",
    ], estilos))
    story.append(_nota(
        "El programa nunca abre tu navegador de internet normal (Chrome, Edge, etc.) — todo pasa "
        "dentro de esa ventana propia. Si en algún momento ves que se abrió el navegador en vez de "
        "la ventana del programa, algo salió mal; revisa la sección \"Qué hacer si algo falla\".",
        estilos,
    ))
    story.append(_nota(
        "<b>Si en cambio estás usando la versión de código fuente</b> (sección anterior), el "
        "inicio es distinto: se hace doble clic en <font face=\"Courier\">run_local.bat</font>, y "
        "ahí sí aparece brevemente una ventana negra de terminal mientras el programa arranca por "
        "dentro — es normal, no hay que cerrarla a la fuerza; unos segundos después se abre la "
        "ventana propia del programa igual que en la versión empaquetada.",
        estilos,
    ))

    # --- 4. Cómo se usa cada módulo --------------------------------------
    story.append(Paragraph("3. Cómo se usa cada módulo", estilos["Seccion"]))

    story.append(Paragraph("3.1 Inicio (Dashboard)", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Es la primera pantalla que ves al iniciar sesión, y puedes volver a ella en cualquier "
        "momento con el enlace \"Inicio\" en la barra de arriba. Muestra tres tarjetas: unidades "
        "vendidas hoy, ganancia neta de hoy (en verde si es positiva, en rojo si es negativa), y "
        "cuántas alertas de conteo físico están activas.",
        estilos["Cuerpo"],
    ))
    story.append(Paragraph(
        "Si hay productos con una diferencia de conteo sin resolver, aparecen primero, arriba de "
        "todo, en una sección \"Requieren atención\" — antes que la tabla normal de productos, sin "
        "importar su categoría:",
        estilos["Cuerpo"],
    ))
    story.append(_lista([
        "Fila <b>roja</b> = <b>faltante</b> (hay menos botellas de las que el sistema esperaba).",
        "Fila <b>azul</b> = <b>sobrante</b> (hay más botellas de las que el sistema esperaba).",
    ], estilos))
    story.append(Paragraph(
        "En cualquiera de los dos casos, el botón \"Revisar\" te lleva al detalle de ese conteo, "
        "donde decides si generar el ajuste (ver la sección 3.4).",
        estilos["Cuerpo"],
    ))

    story.append(Paragraph("3.2 Registrar entrada", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Se usa cada vez que llega una compra de bebidas del proveedor. Ejemplo concreto:",
        estilos["Cuerpo"],
    ))
    story.append(_nota(
        "<b>Ejemplo:</b> recibiste 24 cervezas Gallo a Q12.00 cada una.<br/>"
        "1. Ve a \"Registrar entrada\".<br/>"
        "2. Producto: <i>Gallo</i>.<br/>"
        "3. Fecha: la fecha en que llegó la compra.<br/>"
        "4. Cantidad: <i>24</i>.<br/>"
        "5. Costo unitario: <i>Q12.00</i>.<br/>"
        "6. Proveedor (opcional): el nombre del proveedor, si quieres dejarlo anotado.<br/>"
        "7. Presiona \"Guardar\".<br/>"
        "El sistema combina automáticamente esta compra con las anteriores para calcular el "
        "costo promedio ponderado del producto — no necesitas calcular nada a mano, ni aunque "
        "el costo haya sido distinto la última vez.",
        estilos,
    ))

    story.append(Paragraph("3.3 Registrar salida (venta / merma / ajuste)", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Cualquier cosa que reduce el inventario de botellas se registra aquí, eligiendo uno de "
        "tres tipos:",
        estilos["Cuerpo"],
    ))
    story.append(_lista([
        "<b>Venta</b> — botellas que se vendieron. Se valúan al precio de venta actual del producto.",
        "<b>Merma</b> — botellas rotas, derramadas o vencidas, sin haberse vendido. Se valúan al "
        "costo (es una pérdida, no genera ingreso).",
        "<b>Ajuste</b> — corrección manual del stock fuera de un conteo físico. En la práctica casi "
        "nunca se usa a mano: los ajustes se generan solos desde la pantalla de un conteo físico.",
    ], estilos))
    story.append(Paragraph(
        "Para <b>merma</b> y <b>ajuste</b> el campo Motivo es obligatorio: el sistema no deja "
        "guardar sin explicar por qué (por ejemplo, \"3 botellas rotas al descargar el camión\"). "
        "Es obligatorio a propósito, porque ese motivo queda como respaldo permanente si alguien "
        "revisa los números después — una pérdida sin explicación es justo lo que una auditoría "
        "debe evitar.",
        estilos["Cuerpo"],
    ))

    story.append(Paragraph("3.4 Conteo físico (el corazón de la auditoría)", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Se recomienda hacer un conteo físico <b>una vez por semana</b> por producto. Consiste en "
        "contar a mano las botellas reales y anotar ese número — el sistema lo compara contra el "
        "\"stock teórico\" (lo que debería haber según todas las compras y salidas registradas).",
        estilos["Cuerpo"],
    ))
    story.append(_nota(
        "<b>Muy importante — resolución manual, nunca automática:</b> al guardar un conteo físico, "
        "el sistema <b>no</b> cambia el stock ni genera ningún ajuste por su cuenta. Solo calcula y "
        "muestra la diferencia, y la deja como una alerta activa. El stock del sistema únicamente "
        "cambia si tú, el auditor, revisas esa diferencia y presionas a propósito el botón "
        "\"Generar ajuste\". Mientras no lo hagas, la alerta se mantiene — así te aseguras de que "
        "cada diferencia real quede revisada por una persona, nunca aprobada sola.",
        estilos,
    ))
    story.append(_nota(
        "<b>Ejemplo:</b> el stock teórico de Corona es 100 botellas, pero al contar físicamente "
        "solo hay 92.<br/>"
        "1. Registras el conteo con cantidad contada: <i>92</i>.<br/>"
        "2. El sistema muestra: diferencia = <i>-8</i> (faltante), en rojo.<br/>"
        "3. Revisas si tiene sentido (¿se rompió algo sin registrar? ¿hubo un error al contar?).<br/>"
        "4. Si decides que el faltante es real, presionas \"Generar ajuste\" — recién ahí el stock "
        "del sistema baja a 92 y queda un registro permanente de ese ajuste.",
        estilos,
    ))
    story.append(Paragraph(
        "Al guardar un conteo físico, el sistema te regresa a la pantalla de Inicio — no te lleva "
        "directo a revisarlo. Así decides tú mismo, uno por uno, en qué momento revisar cada "
        "alerta pendiente.",
        estilos["Cuerpo"],
    ))

    story.append(Paragraph("3.5 Historial", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Muestra, mezclados en orden cronológico, absolutamente todo lo registrado: cada entrada, "
        "cada venta, cada merma, cada ajuste (sobrante o faltante), y cada conteo físico — se haya "
        "generado su ajuste o no. Se puede filtrar por producto, por rango de fechas, o ambos a la "
        "vez; sin filtros, se muestra todo.",
        estilos["Cuerpo"],
    ))

    story.append(Paragraph("3.6 Reportes", estilos["SubSeccion"]))
    story.append(Paragraph(
        "El orden para generar un reporte es: primero se eligen los <b>productos</b> a incluir "
        "(o se deja todo sin marcar para incluir todos los productos activos), y después el "
        "<b>periodo</b> (Hoy / Esta semana / Este mes, o un rango de fechas personalizado). El "
        "reporte no se genera hasta que se elige un periodo — si se intenta antes, el sistema "
        "muestra un aviso pidiendo elegir uno.",
        estilos["Cuerpo"],
    ))
    story.append(Paragraph(
        "En pantalla aparecen tarjetas de resumen y gráficas (ganancia por producto, y ventas por "
        "día si el rango cubre más de un día). El botón \"Descargar Excel\" al final genera un "
        "archivo con dos hojas: un resumen financiero por producto, y el detalle cronológico "
        "completo de movimientos del periodo elegido.",
        estilos["Cuerpo"],
    ))
    story.append(_nota(
        "Como todo queda guardado permanentemente en la base de datos, se puede consultar "
        "cualquier mes pasado en cualquier momento — por ejemplo, generar el reporte de junio "
        "estando ya en octubre — y los números de un mes ya cerrado no cambian con el tiempo.",
        estilos,
    ))

    story.append(Paragraph("3.7 Productos y categorías", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Desde \"Categorías\" y \"Productos\" en la barra de arriba se da de alta o se edita el "
        "catálogo de bebidas que se audita. En vez de borrar un producto que ya no se vende, se "
        "usa el botón \"Desactivar\" — así deja de aparecer en el Inicio y en los formularios "
        "nuevos, pero su historial queda intacto para siempre. Normalmente esto se configura una "
        "sola vez al empezar y solo se ajusta cuando cambia el menú de bebidas del restaurante.",
        estilos["Cuerpo"],
    ))

    # --- 5. Cómo se cierra correctamente ---------------------------------
    story.append(Paragraph("4. Cómo se cierra el programa correctamente", estilos["Seccion"]))
    story.append(_lista_numerada([
        "Cierra la ventana del programa con el botón de cerrar normal (la X de la ventana), "
        "como cualquier otro programa de Windows.",
        "Eso es todo — no hace falta ningún paso adicional.",
    ], estilos))
    story.append(_nota(
        "<b>No</b> es necesario, ni recomendable, forzar el cierre desde el Administrador de "
        "tareas de Windows. Al cerrar la ventana normalmente, el programa libera automáticamente "
        "el puerto local que estaba usando y no deja ningún proceso corriendo en segundo plano. "
        "Forzar el cierre desde el Administrador de tareas puede dejar el puerto ocupado, lo que "
        "impediría abrir el programa de nuevo hasta reiniciar el equipo.",
        estilos,
    ))

    # --- 6. Qué hacer si algo falla ---------------------------------------
    story.append(Paragraph("5. Qué hacer si algo falla", estilos["Seccion"]))

    story.append(Paragraph("No se abre ninguna ventana", estilos["SubSeccion"]))
    story.append(_lista([
        "Espera unos segundos más — la primera vez que abre puede tardar un poco.",
        "Confirma que copiaste la carpeta <font face=\"Courier\">AuditoriaAylupita</font> "
        "<b>completa</b> (no solo el <font face=\"Courier\">.exe</font> suelto, separado de los "
        "archivos que lo acompañan).",
        "(Solo si usas la versión de código fuente) revisa si quedó abierta la ventana negra de "
        "terminal detrás de otras ventanas — puede tener un mensaje de error explicando qué pasó. "
        "Si menciona que el entorno virtual (\"venv\") no existe, repite los pasos de instalación "
        "de la sección 1.",
    ], estilos))

    story.append(Paragraph("La aplicación no responde / se ve congelada", estilos["SubSeccion"]))
    story.append(_lista([
        "Cierra la ventana con la X y vuelve a abrir el programa (sección 2).",
        "Si tampoco abre después de cerrar, reinicia el equipo — eso libera cualquier proceso que "
        "haya quedado colgado — y vuelve a intentar.",
    ], estilos))

    story.append(Paragraph("Necesito restaurar una copia local de la base (modo sin nube)", estilos["SubSeccion"]))
    story.append(Paragraph(
        "Si el programa está corriendo con una base de datos local (sin conexión a la nube), cada "
        "vez que se abre guarda automáticamente una copia de seguridad en una carpeta "
        "<font face=\"Courier\">backups/</font> junto al programa (junto al "
        "<font face=\"Courier\">.exe</font> en la versión empaquetada, o en la carpeta del proyecto "
        "en la versión de código fuente). Cada archivo tiene la fecha y hora en el nombre "
        "(ej. <font face=\"Courier\">db_20260825_103000.sqlite3</font>), de más antiguo a más reciente.",
        estilos["Cuerpo"],
    ))
    story.append(_lista_numerada([
        "Cierra el programa por completo (sección 4).",
        "Ve a la carpeta <font face=\"Courier\">backups/</font> y elige el respaldo con la fecha "
        "de antes de que ocurriera el problema.",
        "Haz una copia de ese archivo, y renómbrala a <font face=\"Courier\">db.sqlite3</font>.",
        "Colócala junto al programa (junto al <font face=\"Courier\">.exe</font>, o en la carpeta "
        "principal del proyecto), reemplazando el <font face=\"Courier\">db.sqlite3</font> actual.",
        "Vuelve a abrir el programa normalmente.",
    ], estilos))
    story.append(_nota(
        "Restaurar un respaldo es una operación que conviene hacer con calma y, si es posible, "
        "con ayuda del administrador del sistema — reemplaza todos los datos registrados después "
        "de la fecha de ese respaldo.",
        estilos,
    ))

    story.append(Paragraph("¿Qué pasa si se pierde el acceso a la base de datos en la nube?", estilos["SubSeccion"]))
    story.append(Paragraph(
        "El sistema guarda toda la información real (productos, movimientos, usuarios) en una base "
        "de datos en la nube. Como respaldo adicional, cada vez que un administrador o auditor "
        "inicia sesión por primera vez en el día, el programa guarda automáticamente una copia "
        "completa de todo el sistema en la carpeta <font face=\"Courier\">backups_completos/</font> "
        "junto al programa. Si algún día se pierde el acceso a la base de datos en la nube — por "
        "ejemplo, si la cuenta del servicio en la nube deja de funcionar o la conexión se corta de "
        "forma permanente — ahí hay copias recientes de todo el sistema que se pueden usar para "
        "reconstruirlo desde cero.",
        estilos["Cuerpo"],
    ))
    story.append(_nota(
        "Restaurar desde una de estas copias es una tarea técnica (no es simplemente reemplazar un "
        "archivo, como con el respaldo local de arriba) — si alguna vez hace falta, contacta al "
        "administrador del sistema o a quien haya desarrollado el programa. Lo importante para el "
        "día a día es solo saber que esa carpeta existe y que no hay que borrarla.",
        estilos,
    ))
    story.append(Paragraph(
        "El programa guarda como máximo una copia por día en esa carpeta (no se acumulan copias "
        "repetidas si se abre y se cierra varias veces el mismo día) y borra automáticamente las "
        "copias de más de 30 días, para que la carpeta no crezca sin control.",
        estilos["Cuerpo"],
    ))

    story.append(Spacer(1, 1 * cm))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#ddd2c2"), thickness=0.8))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "Este manual no incluye usuarios ni contraseñas reales. Tu acceso te lo entrega el "
        "administrador del sistema por separado.",
        estilos["CuerpoNota"],
    ))
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(
        "Desarrollado por Julio Orellana | julioes134@outlook.es<br/>"
        "Todos los derechos reservados. Se prohíbe la utilización de este sistema sin la "
        "autorización debida; hacerlo podría constituir un delito de derechos de autor.",
        estilos["CuerpoNota"],
    ))

    return story


def _dibujar_pie(canvas_obj, documento):
    """Pie de página discreto en cada hoja: firma y aviso de derechos reservados."""
    canvas_obj.saveState()
    canvas_obj.setFont("Helvetica", 7)
    canvas_obj.setFillColor(INK_SOFT)
    ancho_pagina = documento.pagesize[0]
    texto = (
        "Desarrollado por Julio Orellana | julioes134@outlook.es · "
        "Todos los derechos reservados — se prohíbe la utilización de este sistema sin autorización debida."
    )
    canvas_obj.drawCentredString(ancho_pagina / 2, 1.1 * cm, texto)
    canvas_obj.restoreState()


class Command(BaseCommand):
    help = "Genera MANUAL_USUARIO.pdf en la raíz del proyecto, con reportlab."

    def handle(self, *args, **options):
        ruta_salida = settings.BASE_DIR / "MANUAL_USUARIO.pdf"

        estilos = _construir_estilos()
        documento = SimpleDocTemplate(
            str(ruta_salida),
            pagesize=letter,
            topMargin=2.2 * cm,
            bottomMargin=2 * cm,
            leftMargin=2.2 * cm,
            rightMargin=2.2 * cm,
            title="Manual de Usuario — Auditoría Aylupita",
        )
        documento.build(
            _construir_contenido(estilos),
            onFirstPage=_dibujar_pie,
            onLaterPages=_dibujar_pie,
        )

        self.stdout.write(self.style.SUCCESS(f"Manual generado: {ruta_salida}"))

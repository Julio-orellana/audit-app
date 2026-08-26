# inventario/antiduplicado.py
"""
Protección de doble-submit para formularios de creación (prompt 21,
punto 2): un doble clic, una recarga-y-reenvío, o dos pestañas con el
mismo formulario no deben crear dos filas.

Capa de frontend (deshabilitar el botón al enviar, ver templates/base.html)
reduce la probabilidad pero no es confiable por sí sola — el usuario puede
recargar y reenviar, o el JS puede fallar. Esta es la capa de backend, que
es la que de verdad importa.

Patrón: cada GET que muestra el formulario genera un token de un solo uso
(un valor aleatorio, no un secreto — no reemplaza al CSRF token, lo
acompaña). El POST solo procesa el formulario si logra "reservar" ese
token en el cache con cache.add(), que es atómico: si dos requests casi
simultáneos llegan con el mismo token (el navegador reenviando el mismo
formulario, o dos clics que dispararon dos requests), como máximo uno
de los dos logra reservarlo — el otro ve que ya estaba reservado y no
crea un segundo registro. A diferencia de "leer de la sesión y luego
borrar" (que tiene el mismo hueco de carrera que el bug original de
generar_ajuste), cache.add() no da esa ventana: la reserva y la
verificación son la misma operación atómica.
"""
import uuid

from django.contrib import messages
from django.core.cache import cache

TOKEN_CAMPO = "token_formulario"
TOKEN_TIMEOUT_SEGUNDOS = 300  # 5 minutos: de sobra para un doble clic o una recarga tardía, corto para no acumular basura en el cache indefinidamente


class ProteccionDobleSubmitMixin:
    """
    Mixin para CreateView: agrega `token_formulario` al contexto en cada
    render (tanto el GET inicial como un re-render con errores de
    validación) y lo exige — consumiéndolo de forma atómica — justo antes
    de guardar, en form_valid(). Un reenvío con un token ya usado (o sin
    token) no crea un registro nuevo — se redirige como si hubiera
    funcionado, sin mensaje de error, porque desde la perspectiva del
    usuario su envío original sí funcionó.

    A propósito NO se verifica en post() sino en form_valid(): así un
    envío que falla la validación (ej. le faltó el motivo) no "quema" el
    token — el re-render le da uno nuevo y su reintento corregido no
    queda bloqueado por accidente. El caso que sí importa proteger (dos
    envíos casi simultáneos con datos VÁLIDOS y el mismo token) sigue
    protegido igual, porque cache.add() es atómico: de dos form_valid()
    corriendo en threads distintos con el mismo token, como máximo uno
    logra reservarlo.
    """

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context[TOKEN_CAMPO] = uuid.uuid4().hex
        return context

    def form_valid(self, form):
        token = self.request.POST.get(TOKEN_CAMPO, "")
        clave_cache = f"form_token_usado:{token}"
        token_reservado_ahora = bool(token) and cache.add(clave_cache, True, TOKEN_TIMEOUT_SEGUNDOS)
        if not token_reservado_ahora:
            messages.info(self.request, "Este formulario ya se había enviado — no se creó un registro duplicado.")
            return self.redirigir_tras_duplicado()
        return super().form_valid(form)

    def redirigir_tras_duplicado(self):
        """
        A dónde mandar un reenvío duplicado detectado. self.object nunca
        se llega a crear en este caso, así que no se puede usar
        get_success_url() de Django tal cual — depende de self.object en
        varias vistas genéricas. Por default usa el success_url estático
        de la vista; las vistas cuyo destino depende de otra cosa (ej.
        MovimientoSalidaCreateView, que depende del rol) sobreescriben
        este método.
        """
        from django.shortcuts import redirect
        return redirect(self.success_url)

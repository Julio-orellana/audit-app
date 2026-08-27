# inventario/sync.py
"""
OBSOLETO — no lo importa nadie; se conserva solo como registro de una
decisión de diseño.

Este módulo se creó en el prompt 18 como punto de enganche para el motor
de sincronización offline, con la idea de interceptar las escrituras con
señales de Django (post_save/post_delete). Al implementarlo de verdad
(prompt 19) esa idea NO se usó, por una razón concreta: post_save solo se
dispara DESPUÉS de que la fila se guardó. Sin conexión, la escritura
contra Neon falla antes de eso, así que la señal nunca llega a
dispararse — justo en el único escenario que el motor tiene que resolver.

La intercepción real ocurre una capa más arriba, en la vista: ver
ColaOfflineMixin en inventario/offline.py, que encola en la base local
ANTES de intentar la escritura remota.
"""

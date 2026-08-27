"""
URL configuration for auditoria_aylupita project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from inventario.views import LoginConRespaldoOfflineView, LogoutConRespaldoOfflineView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Login/logout propios (prompt 19b, punto 1): iniciar sesión SÍ
    # funciona sin conexión, validando contra la caché local de
    # credenciales de esta máquina — ver inventario/offline.py.
    path('login/', LoginConRespaldoOfflineView.as_view(), name='login'),
    path('logout/', LogoutConRespaldoOfflineView.as_view(), name='logout'),
    path('', include('inventario.urls')),
]

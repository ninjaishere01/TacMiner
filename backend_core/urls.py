from django.contrib import admin
from django.urls import path, include

admin.site.site_header = "TAC Miner"
admin.site.site_title = "TAC Miner Admin"
admin.site.index_title = "لوحة التحكم"


urlpatterns = [
    path('admin-is-none/', admin.site.urls),
    path('' , include('miner_app.urls')),
]

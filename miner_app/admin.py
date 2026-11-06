from django.contrib import admin
from miner_app.models import Worker, User, UserWorker, WithdrawRequest #



admin.site.register(Worker)
admin.site.register(UserWorker)
admin.site.register(User)


@admin.register(WithdrawRequest)
class WithdrawRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'wallet_address', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'wallet_address')

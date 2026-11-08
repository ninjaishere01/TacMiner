from django.contrib import admin
from miner_app.models import Worker, User, UserWorker, WithdrawRequest #



admin.site.register(Worker)
admin.site.register(UserWorker)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "referrals_count", "telegram_id")
    search_fields = ("username", "telegram_id")

    @admin.display(description="عدد الدعوات")
    def referrals_count(self, obj):
        return obj.referrals.count()


@admin.register(WithdrawRequest)
class WithdrawRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'wallet_address', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'wallet_address')

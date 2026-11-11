from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import path, reverse
from miner_app.models import Worker, User, UserWorker, WithdrawRequest
from miner_app.views import complete_withdraw_request, reject_withdraw_request



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
    list_display = ('user', 'amount', 'wallet_address', 'status', 'note' , 'created_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'wallet_address', 'note')
    readonly_fields = ('user', 'amount', 'wallet_address', 'status', 'note', 'created_at')
    actions = ('mark_paid', 'mark_rejected')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom_urls = [
            path(
                "decide/",
                self.admin_site.admin_view(self.decide_view),
                name="miner_app_withdrawrequest_decide",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.action(description="تعليم الطلبات المختارة كمدفوعة")
    def mark_paid(self, request, queryset):
        return self._redirect_to_decide(request, queryset, "paid")

    @admin.action(description="رفض الطلبات المختارة وإرجاع المبلغ بالـ TAC")
    def mark_rejected(self, request, queryset):
        return self._redirect_to_decide(request, queryset, "rejected")

    def _redirect_to_decide(self, request, queryset, decision):
        ids = ",".join(str(pk) for pk in queryset.values_list("pk", flat=True))
        url = reverse("admin:miner_app_withdrawrequest_decide")
        return redirect(f"{url}?decision={decision}&ids={ids}")

    def decide_view(self, request):
        if not self.has_change_permission(request):
            raise PermissionDenied

        decision = request.POST.get("decision") or request.GET.get("decision")
        if decision not in {"paid", "rejected"}:
            messages.error(request, "قرار غير معروف.")
            return redirect("..")

        raw_ids = request.POST.get("ids") or request.GET.get("ids") or ""
        ids = [int(value) for value in raw_ids.split(",") if value.strip().isdigit()]
        withdrawals = WithdrawRequest.objects.filter(
            pk__in=ids, status="pending"
        ).select_related("user")

        if not withdrawals:
            messages.warning(request, "لا توجد طلبات معلقة في الاختيار.")
            return redirect("..")

        value_label = "مرجع عملية الدفع" if decision == "paid" else "سبب الرفض"

        if request.method == "POST":
            value = (request.POST.get("value") or "").strip()
            if not value:
                messages.error(request, f"{value_label} مطلوب.")
            else:
                processed = 0
                for withdrawal in withdrawals:
                    if decision == "paid":
                        completed = complete_withdraw_request(
                            withdrawal, payment_reference=value
                        )
                    else:
                        completed = reject_withdraw_request(
                            withdrawal, reason=value
                        )
                    processed += 1 if completed else 0
                messages.success(request, f"تم تنفيذ العملية على {processed} طلب.")
                return redirect("..")

        context = {
            **self.admin_site.each_context(request),
            "title": "تأكيد قرار السحب",
            "opts": self.model._meta,
            "decision": decision,
            "decision_label": "تعليم كمدفوع" if decision == "paid" else "رفض وإرجاع المبلغ",
            "value_label": value_label,
            "withdrawals": withdrawals,
            "ids": ",".join(
                str(pk) for pk in withdrawals.values_list("pk", flat=True)
            ),
        }
        return render(
            request,
            "admin/miner_app/withdrawrequest/decide.html",
            context,
        )

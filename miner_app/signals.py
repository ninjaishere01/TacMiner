from decimal import Decimal

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from miner_app.models import WithdrawRequest


@receiver(pre_save, sender=WithdrawRequest)
def remember_previous_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status = None
        return
    instance._previous_status = (
        sender.objects.filter(pk=instance.pk)
        .values_list("status", flat=True)
        .first()
    )


@receiver(post_save, sender=WithdrawRequest)
def announce_paid_withdrawal(sender, instance, created, **kwargs):
    if created or instance.status != "completed":
        return
    if getattr(instance, "_previous_status", None) == "completed":
        return

    from miner_app import tasks, views

    tac_amount = (instance.amount * views.WITHDRAW_TAC_PER_USDT).quantize(
        Decimal("0.00000001")
    )
    views.enqueue_task(
        tasks.send_withdrawal_proof,
        instance.user.username,
        tac_amount,
        instance.amount,
        instance.pk,
        instance.wallet_address,
        "completed",
        instance.user.telegram_id,
    )

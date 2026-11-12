from celery import shared_task

RETRY_POLICY = {
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "retry_jitter": True,
    "max_retries": 3,
}


@shared_task(**RETRY_POLICY)
def send_withdrawal_proof(
    username,
    tac_amount,
    usdt_amount,
    request_id,
    wallet_address="",
    status="pending",
    telegram_id="",
):
    from miner_app.views import send_withdrawal_proof_notification

    send_withdrawal_proof_notification(
        username,
        tac_amount,
        usdt_amount,
        request_id,
        wallet_address,
        status,
        telegram_id,
    )


@shared_task(**RETRY_POLICY)
def send_admin_alert(username, amount, wallet_address):
    from miner_app.views import send_admin_notification

    send_admin_notification(username, amount, wallet_address)


@shared_task(**RETRY_POLICY)
def send_welcome(chat_id):
    from miner_app.views import send_start_welcome

    send_start_welcome(chat_id)


@shared_task(**RETRY_POLICY)
def send_public_announcement(username, amount, wallet_address):
    from miner_app.views import send_public_announcement as post_announcement

    post_announcement(username, amount, wallet_address)

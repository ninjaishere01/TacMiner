import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "يسجّل webhook البوت على الدومين الجديد."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            help="رابط الـ webhook الكامل. الافتراضي: WEBAPP_URL + /api/telegram-webhook/",
        )

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN is not configured")

        webhook_url = options["url"] or (
            f"{settings.WEBAPP_URL.rstrip('/')}/api/telegram-webhook/"
        )
        payload = {
            "url": webhook_url,
            "allowed_updates": ["message", "edited_message"],
        }
        if settings.TELEGRAM_WEBHOOK_SECRET:
            payload["secret_token"] = settings.TELEGRAM_WEBHOOK_SECRET

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json=payload,
                timeout=15,
            )
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CommandError(f"Telegram setWebhook request failed: {exc}") from exc

        if not result.get("ok"):
            raise CommandError(f"Telegram rejected the webhook: {result}")

        self.stdout.write(self.style.SUCCESS(f"Webhook registered: {webhook_url}"))

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from miner_app.models import (
    AdSession,
    PendingReferral,
    User,
    UserTask,
    UserWorker,
    Worker,
    WithdrawRequest,
)
from miner_app.views import (
    complete_withdraw_request,
    reject_withdraw_request,
    send_withdrawal_proof_notification,
)


TEST_BOT_TOKEN = "123456:test-token"
TEST_WEBHOOK_SECRET = "test-webhook-secret"


def build_init_data(telegram_id, auth_date=None, token=TEST_BOT_TOKEN):
    auth_date = int(auth_date if auth_date is not None else time.time())
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAH-test-query",
        "user": json.dumps(
            {"id": telegram_id, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


from backend_core.celery import app as celery_app

# نفّذ مهام Celery فورًا داخل نفس العملية أثناء الاختبارات (بدون Redis worker).
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class ApiSecurityTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def test_stale_init_data_is_rejected(self):
        stale_init_data = build_init_data(
            1101, auth_date=time.time() - 86400 - 120
        )
        response = self.post_json("/api/login/", {"initData": stale_init_data})

        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(telegram_id=1101).exists())

    def test_fresh_init_data_is_accepted(self):
        response = self.post_json(
            "/api/login/",
            {"initData": build_init_data(1102), "username": "fresh user"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(telegram_id=1102).exists())

    def test_oversized_init_data_is_rejected(self):
        response = self.post_json(
            "/api/login/", {"initData": "x" * 10000}
        )

        self.assertEqual(response.status_code, 401)

    def test_webhook_requires_secret(self):
        body = {"message": {"text": "/start ref_500", "from": {"id": 600}}}
        response = self.post_json("/api/telegram-webhook/", body)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(PendingReferral.objects.filter(telegram_id=600).exists())

    def test_webhook_with_secret_records_referral(self):
        body = {"message": {"text": "/start ref_500", "from": {"id": 601}}}
        response = self.post_json(
            "/api/telegram-webhook/",
            body,
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=TEST_WEBHOOK_SECRET,
        )

        self.assertEqual(response.status_code, 200)
        pending = PendingReferral.objects.get(telegram_id=601)
        self.assertEqual(pending.referral_id, 500)

    def test_unknown_task_name_is_rejected_without_reward(self):
        user = User.objects.create(telegram_id=1201, username="unknown-task")

        response = self.post_json(
            "/api/claim-task/",
            {
                "initData": build_init_data(1201),
                "task_name": "free_money_please",
            },
        )

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertEqual(user.balance, Decimal("0"))
        self.assertFalse(UserTask.objects.filter(user=user).exists())

    def test_milestone_20_keeps_existing_reward(self):
        user = User.objects.create(telegram_id=1202, username="milestone20")

        response = self.post_json(
            "/api/claim-task/",
            {"initData": build_init_data(1202), "task_name": "milestone_20"},
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.balance, Decimal("50.00"))
        self.assertTrue(
            UserTask.objects.filter(user=user, task_name="milestone_20").exists()
        )


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class WithdrawRuleTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def create_eligible_user(self, telegram_id=2001, usdt_balance=Decimal("10")):
        user = User.objects.create(
            telegram_id=telegram_id,
            username=f"user{telegram_id}",
            wallet_address="EQTestWalletAddress",
            is_wallet_verified=True,
            usdt_balance=usdt_balance,
        )
        for offset in range(3):
            User.objects.create(
                telegram_id=telegram_id + 100 + offset,
                username=f"friend{offset}",
                referred_by=user,
            )
        return user

    @patch("miner_app.views.send_withdrawal_proof_notification")
    @patch("miner_app.views.send_admin_notification")
    def test_withdrawals_are_allowed_multiple_times_per_day(
        self, notification_mock, proof_mock
    ):
        user = self.create_eligible_user(usdt_balance=Decimal("10"))
        payload = {"initData": build_init_data(user.telegram_id), "amount": 5}

        first = self.post_json("/api/request-withdraw/", payload)
        second = self.post_json("/api/request-withdraw/", payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            WithdrawRequest.objects.filter(user=user, status="pending").count(), 2
        )
        user.refresh_from_db()
        self.assertEqual(user.usdt_balance, Decimal("0"))
        self.assertEqual(proof_mock.call_count, 2)
        (
            _,
            tac_amount,
            usdt_amount,
            request_id,
            wallet_address,
            status,
            telegram_id,
        ) = proof_mock.call_args_list[0].args
        self.assertEqual(tac_amount, Decimal("4600.00000000"))
        self.assertEqual(usdt_amount, Decimal("5"))
        self.assertEqual(wallet_address, "EQTestWalletAddress")
        self.assertEqual(status, "pending")
        self.assertEqual(telegram_id, user.telegram_id)
        self.assertEqual(
            request_id,
            WithdrawRequest.objects.filter(user=user).order_by("id").first().pk,
        )

    def test_reject_refunds_tac_exactly_once(self):
        user = User.objects.create(telegram_id=2101, username="refund-user")
        withdrawal = WithdrawRequest.objects.create(
            user=user,
            amount=Decimal("5"),
            wallet_address="EQTestWalletAddress",
            status="pending",
        )

        first = reject_withdraw_request(withdrawal, reason="عنوان غير صالح")
        second = reject_withdraw_request(withdrawal, reason="محاولة تانية")

        self.assertTrue(first)
        self.assertFalse(second)
        user.refresh_from_db()
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "rejected")
        self.assertEqual(withdrawal.note, "عنوان غير صالح")
        self.assertEqual(user.balance, Decimal("4600.00000000"))

    @patch("miner_app.views.send_withdrawal_proof_notification")
    @patch("miner_app.views.send_public_announcement")
    def test_complete_announces_only_after_paid_and_once(
        self, announce_mock, proof_mock
    ):
        user = User.objects.create(telegram_id=2201, username="paid-user")
        withdrawal = WithdrawRequest.objects.create(
            user=user,
            amount=Decimal("7.5"),
            wallet_address="EQTestWalletAddress",
            status="pending",
        )

        first = complete_withdraw_request(withdrawal, payment_reference="TX123")
        second = complete_withdraw_request(withdrawal, payment_reference="TX456")

        self.assertTrue(first)
        self.assertFalse(second)
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, "completed")
        self.assertIn("TX123", withdrawal.note)
        announce_mock.assert_called_once_with(
            "paid-user", 7.5, "EQTestWalletAddress"
        )
        proof_mock.assert_called_once()
        self.assertEqual(proof_mock.call_args.args[5], "completed")


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
    ADSGRAM_BLOCK_ID="",
)
class SettlementAndSeedTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def test_upgrade_settles_earnings_at_old_rate(self):
        user = User.objects.create(
            telegram_id=3001,
            username="upgrader",
            balance=Decimal("1000"),
            last_collected_at=timezone.now() - timedelta(hours=1),
            last_activation_date=timezone.now().date(),
            ads_watched_today=3,
        )
        worker = Worker.objects.create(
            name="Alpha",
            icon="fa-person-digging",
            image="/static/images/1.png",
            base_price=Decimal("100"),
        )
        user_worker = UserWorker.objects.create(
            user=user, worker=worker, level=1
        )

        old_rate_per_hour = Decimal("100") * Decimal("0.01") * Decimal("1.1")
        price = Decimal("100") * Decimal("1.5")

        response = self.post_json(
            "/api/upgrade/",
            {"initData": build_init_data(3001), "worker_id": worker.id},
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        user_worker.refresh_from_db()
        self.assertEqual(user_worker.level, 2)

        earned = user.balance - Decimal("1000") + price
        self.assertAlmostEqual(
            float(earned), float(old_rate_per_hour), delta=0.05
        )
        self.assertGreater(
            user.last_collected_at, timezone.now() - timedelta(seconds=10)
        )

    def test_upgrade_does_not_settle_when_mining_inactive(self):
        collected_at = timezone.now() - timedelta(hours=1)
        user = User.objects.create(
            telegram_id=3002,
            username="inactive-upgrader",
            balance=Decimal("1000"),
            last_collected_at=collected_at,
        )
        worker = Worker.objects.create(
            name="Hawk",
            icon="fa-crow",
            image="/static/images/3.png",
            base_price=Decimal("100"),
        )
        UserWorker.objects.create(user=user, worker=worker, level=1)

        response = self.post_json(
            "/api/upgrade/",
            {"initData": build_init_data(3002), "worker_id": worker.id},
        )

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.balance, Decimal("850.00"))
        self.assertAlmostEqual(
            user.last_collected_at.timestamp(),
            collected_at.timestamp(),
            delta=0.01,
        )

    def test_seed_workers_does_not_overwrite_prices(self):
        worker = Worker.objects.create(
            name="Alpha",
            icon="fa-person-digging",
            image="/static/images/1.png",
            base_price=Decimal("999.00"),
        )

        call_command("seed_workers", verbosity=0)

        worker.refresh_from_db()
        self.assertEqual(worker.base_price, Decimal("999.00"))
        self.assertEqual(Worker.objects.count(), 8)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class AdminWithdrawActionTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="admin-test-pass",
        )
        self.client.force_login(admin_user)
        self.player = User.objects.create(
            telegram_id=4001,
            username="admin-action-user",
            wallet_address="EQAdminActionWallet",
        )
        self.withdrawal = WithdrawRequest.objects.create(
            user=self.player,
            amount=Decimal("6"),
            wallet_address="EQAdminActionWallet",
            status="pending",
        )

    @patch("miner_app.views.send_withdrawal_proof_notification")
    @patch("miner_app.views.send_public_announcement")
    def test_admin_marks_withdrawal_paid(self, announce_mock, proof_mock):
        decide_url = "/admin/miner_app/withdrawrequest/decide/"

        page = self.client.get(
            decide_url, {"decision": "paid", "ids": str(self.withdrawal.pk)}
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "EQAdminActionWallet")

        response = self.client.post(
            decide_url,
            {
                "decision": "paid",
                "ids": str(self.withdrawal.pk),
                "value": "TX-ADMIN-1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.withdrawal.refresh_from_db()
        self.assertEqual(self.withdrawal.status, "completed")
        self.assertIn("TX-ADMIN-1", self.withdrawal.note)
        announce_mock.assert_called_once()
        proof_mock.assert_called_once()
        self.assertEqual(proof_mock.call_args.args[5], "completed")


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class ActivationCycleTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def create_miner(self, telegram_id, **kwargs):
        defaults = {
            "username": f"miner-{telegram_id}",
            "last_collected_at": timezone.now() - timedelta(hours=1),
        }
        defaults.update(kwargs)
        return User.objects.create(telegram_id=telegram_id, **defaults)

    def add_worker(self, user):
        worker = Worker.objects.create(
            name="Cycle Miner",
            icon="fa-hammer",
            image="/static/images/1.png",
            base_price=Decimal("100"),
        )
        UserWorker.objects.create(user=user, worker=worker, level=1)

    def watch_activation(self, telegram_id):
        return self.post_json(
            "/api/watch-ad/",
            {
                "initData": build_init_data(telegram_id),
                "ad_type": "activation",
            },
        )

    def test_three_ads_start_24_hour_window(self):
        self.create_miner(4101)

        for expected in (1, 2):
            response = self.watch_activation(4101)
            body = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["watched_count"], expected)
            self.assertFalse(body["mining_active"])

        response = self.watch_activation(4101)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["mining_active"])
        self.assertAlmostEqual(body["activation_time_left"], 86400, delta=5)

        user = User.objects.get(telegram_id=4101)
        self.assertEqual(user.ads_watched_today, 3)
        self.assertIsNotNone(user.last_activation_at)

        collect = self.post_json(
            "/api/collect/", {"initData": build_init_data(4101)}
        )
        self.assertEqual(collect.status_code, 200)

    def test_mining_stops_after_24_hours(self):
        expired_at = timezone.now() - timedelta(hours=24, minutes=1)
        user = self.create_miner(
            4102,
            ads_watched_today=3,
            last_activation_date=timezone.now().date(),
            last_activation_at=expired_at,
            last_collected_at=expired_at,
        )
        self.add_worker(user)
        balance_before = user.balance

        response = self.post_json(
            "/api/collect/", {"initData": build_init_data(4102)}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("انتهى", response.json()["error"])
        user.refresh_from_db()
        self.assertEqual(user.balance, balance_before)

    def test_rewatching_ads_credits_previous_window_until_expiry_only(self):
        expiry = timezone.now() - timedelta(hours=2)
        started = expiry - timedelta(hours=24)
        user = self.create_miner(
            4103,
            ads_watched_today=3,
            last_activation_date=started.date(),
            last_activation_at=started,
            last_collected_at=started,
        )
        self.add_worker(user)

        first = self.watch_activation(4103)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["watched_count"], 1)
        self.assertFalse(first.json()["mining_active"])

        user.refresh_from_db()
        self.assertIsNone(user.last_activation_at)
        # 24 ساعة تفعيل فقط بدون احتساب فترة التوقف
        self.assertAlmostEqual(float(user.balance), 24 * 1.1, delta=0.05)

        self.watch_activation(4103)
        third = self.watch_activation(4103)
        self.assertEqual(third.status_code, 200)
        self.assertTrue(third.json()["mining_active"])
        self.assertAlmostEqual(third.json()["activation_time_left"], 86400, delta=5)

        user.refresh_from_db()
        self.assertEqual(user.ads_watched_today, 3)
        self.assertAlmostEqual(
            (timezone.now() - user.last_activation_at).total_seconds(),
            0,
            delta=10,
        )

    def test_cannot_watch_activation_while_active(self):
        now = timezone.now()
        user = self.create_miner(
            4104,
            ads_watched_today=3,
            last_activation_date=now.date(),
            last_activation_at=now - timedelta(hours=1),
            last_collected_at=now - timedelta(hours=1),
        )

        response = self.watch_activation(4104)

        self.assertEqual(response.status_code, 400)
        self.assertGreater(response.json()["activation_time_left"], 0)
        user.refresh_from_db()
        self.assertEqual(user.ads_watched_today, 3)
        self.assertIsNotNone(user.last_activation_at)

    def test_login_reports_active_window(self):
        now = timezone.now()
        self.create_miner(
            4105,
            ads_watched_today=3,
            last_activation_date=now.date(),
            last_activation_at=now - timedelta(hours=1),
            last_collected_at=now - timedelta(hours=1),
        )

        response = self.post_json(
            "/api/login/",
            {"initData": build_init_data(4105), "username": "miner-4105"},
        )

        self.assertEqual(response.status_code, 200)
        user_data = response.json()["user"]
        self.assertTrue(user_data["mining_active"])
        self.assertEqual(user_data["ads_watched_today"], 3)
        self.assertAlmostEqual(
            user_data["activation_time_left"], 23 * 3600, delta=10
        )

    def test_legacy_active_user_gets_24_hour_window_on_login(self):
        self.create_miner(
            4106,
            ads_watched_today=3,
            last_activation_date=timezone.now().date(),
            last_activation_at=None,
        )

        response = self.post_json(
            "/api/login/",
            {"initData": build_init_data(4106), "username": "miner-4106"},
        )

        user_data = response.json()["user"]
        self.assertTrue(user_data["mining_active"])
        user = User.objects.get(telegram_id=4106)
        self.assertIsNotNone(user.last_activation_at)
        self.assertAlmostEqual(
            user_data["activation_time_left"], 86400, delta=10
        )


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class BoostCycleTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def create_miner(self, telegram_id, **kwargs):
        defaults = {
            "username": f"boost-{telegram_id}",
            "last_collected_at": timezone.now() - timedelta(hours=1),
        }
        defaults.update(kwargs)
        return User.objects.create(telegram_id=telegram_id, **defaults)

    def watch_boost(self, telegram_id):
        return self.post_json(
            "/api/watch-ad/",
            {
                "initData": build_init_data(telegram_id),
                "ad_type": "boost",
            },
        )

    def test_fifth_ad_activates_boost_immediately(self):
        self.create_miner(5101)

        for expected in (1, 2, 3, 4):
            response = self.watch_boost(5101)
            body = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["message"], "Boost Ad Watched!")
            self.assertEqual(body["watched_count"], expected)

        response = self.watch_boost(5101)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["message"], "Boost Activated!")
        self.assertEqual(body["watched_count"], 5)
        self.assertEqual(body["boost_multiplier"], 2.0)

        user = User.objects.get(telegram_id=5101)
        self.assertEqual(user.boost_ads_watched, 5)
        self.assertEqual(user.boost_multiplier, Decimal("2.00"))
        self.assertIsNotNone(user.last_boost_time)
        self.assertEqual(self.watch_boost(5101).status_code, 400)

    def test_boost_blocked_during_cooldown(self):
        self.create_miner(
            5102,
            boost_ads_watched=5,
            boost_multiplier=Decimal("1.00"),
            last_boost_time=timezone.now() - timedelta(hours=2),
        )

        response = self.watch_boost(5102)

        self.assertEqual(response.status_code, 400)
        self.assertIn("لازم تستنى", response.json()["error"])

    def test_boost_reopens_after_eight_hours(self):
        self.create_miner(
            5103,
            boost_ads_watched=5,
            boost_multiplier=Decimal("2.00"),
            last_boost_time=timezone.now() - timedelta(hours=8, minutes=1),
        )

        response = self.watch_boost(5103)
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["message"], "Boost Ad Watched!")
        self.assertEqual(body["watched_count"], 1)

        user = User.objects.get(telegram_id=5103)
        self.assertEqual(user.boost_multiplier, Decimal("1.00"))
        self.assertEqual(user.boost_ads_watched, 1)

    def test_login_reports_cooldown_left(self):
        self.create_miner(
            5104,
            boost_ads_watched=5,
            boost_multiplier=Decimal("1.00"),
            last_boost_time=timezone.now() - timedelta(hours=2),
        )

        response = self.post_json(
            "/api/login/",
            {"initData": build_init_data(5104), "username": "boost-5104"},
        )
        user_data = response.json()["user"]

        self.assertEqual(user_data["boost_ads_watched"], 5)
        self.assertEqual(user_data["time_left"], 0)
        self.assertAlmostEqual(user_data["boost_cooldown_left"], 6 * 3600, delta=10)

    def test_login_resets_legacy_stuck_five_ads(self):
        self.create_miner(
            5105,
            boost_ads_watched=5,
            boost_multiplier=Decimal("1.00"),
            last_boost_time=None,
        )

        response = self.post_json(
            "/api/login/",
            {"initData": build_init_data(5105), "username": "boost-5105"},
        )
        user_data = response.json()["user"]

        self.assertEqual(user_data["boost_ads_watched"], 0)
        self.assertEqual(user_data["boost_cooldown_left"], 0)
        user = User.objects.get(telegram_id=5105)
        self.assertEqual(user.boost_ads_watched, 0)


class FakeTelegramResponse:
    def __init__(self, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return {"ok": self.ok}


TEST_WEBAPP_URL = "https://example.test/app"


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    WEBAPP_URL=TEST_WEBAPP_URL,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class TelegramStartWelcomeTests(TestCase):
    expected_caption = (
        "💎 TAC MINING 💎\n\n"
        "Your mining crew is ready!\n\n"
        "⛏ Mine TAC every day\n"
        "⚒ Upgrade your workers to earn more\n"
        "🎁 Complete tasks & claim daily rewards\n"
        "💸 Withdraw safely at 4600 TAC ($5)\n\n"
        "Start mining now 👇"
    )

    def post_update(self, update):
        return self.client.post(
            "/api/telegram-webhook/",
            data=json.dumps(update),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=TEST_WEBHOOK_SECRET,
        )

    def start_update(self, text="/start", chat_type="private", chat_id=7001):
        return {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "text": text,
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": chat_id, "first_name": "Test"},
            },
        }

    @patch("miner_app.views.requests.post")
    def test_start_sends_photo_with_caption_and_webapp_button(self, post_mock):
        post_mock.return_value = FakeTelegramResponse(True)

        response = self.post_update(self.start_update())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post_mock.call_count, 1)
        self.assertTrue(post_mock.call_args.args[0].endswith("/sendPhoto"))
        kwargs = post_mock.call_args.kwargs
        self.assertEqual(kwargs["data"]["chat_id"], 7001)
        self.assertEqual(kwargs["data"]["caption"], self.expected_caption)
        self.assertNotIn("parse_mode", kwargs["data"])
        self.assertEqual(kwargs["files"]["photo"][0], "welcome.jpg")

        markup = json.loads(kwargs["data"]["reply_markup"])
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "⛏ Start Mining")
        self.assertEqual(button["web_app"]["url"], TEST_WEBAPP_URL)

    @patch("miner_app.views.requests.post")
    def test_start_falls_back_to_text_when_photo_fails(self, post_mock):
        post_mock.side_effect = [
            Exception("telegram unavailable"),
            FakeTelegramResponse(True),
        ]

        response = self.post_update(self.start_update())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post_mock.call_count, 2)
        self.assertTrue(post_mock.call_args_list[0].args[0].endswith("/sendPhoto"))
        self.assertTrue(post_mock.call_args_list[1].args[0].endswith("/sendMessage"))

        kwargs = post_mock.call_args_list[1].kwargs
        self.assertEqual(kwargs["json"]["text"], self.expected_caption)
        self.assertNotIn("parse_mode", kwargs["json"])
        button = kwargs["json"]["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "⛏ Start Mining")
        self.assertEqual(button["web_app"]["url"], TEST_WEBAPP_URL)

    @patch("miner_app.views.requests.post")
    @patch("miner_app.views.os.path.isfile", return_value=False)
    def test_start_falls_back_to_text_when_photo_file_is_missing(
        self, _isfile_mock, post_mock
    ):
        post_mock.return_value = FakeTelegramResponse(True)

        response = self.post_update(self.start_update())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post_mock.call_count, 1)
        self.assertTrue(post_mock.call_args.args[0].endswith("/sendMessage"))
        self.assertEqual(
            post_mock.call_args.kwargs["json"]["text"], self.expected_caption
        )

    @patch("miner_app.views.requests.post")
    def test_start_with_referral_keeps_pending_referral_and_sends_welcome(
        self, post_mock
    ):
        post_mock.return_value = FakeTelegramResponse(True)

        response = self.post_update(self.start_update(text="/start ref_9001"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PendingReferral.objects.filter(
                telegram_id=7001, referral_id=9001
            ).exists()
        )
        self.assertEqual(post_mock.call_count, 1)
        self.assertTrue(post_mock.call_args.args[0].endswith("/sendPhoto"))

    @patch("miner_app.views.requests.post")
    def test_other_messages_are_ignored(self, post_mock):
        response = self.post_update(self.start_update(text="hello"))

        self.assertEqual(response.status_code, 200)
        post_mock.assert_not_called()

    @patch("miner_app.views.requests.post")
    def test_group_start_is_ignored(self, post_mock):
        response = self.post_update(
            self.start_update(chat_type="supergroup", chat_id=-1001)
        )

        self.assertEqual(response.status_code, 200)
        post_mock.assert_not_called()


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_WITHDRAWAL_GROUP_ID="@Tac_mining_withdrawal",
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
)
class WithdrawalProofNotificationTests(TestCase):
    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    @patch("miner_app.views.requests.post")
    def test_proof_message_masks_username_and_shows_amounts(self, post_mock):
        post_mock.return_value = FakeTelegramResponse(True)

        send_withdrawal_proof_notification(
            "CryptoMiner",
            Decimal("4600.00000000"),
            Decimal("5"),
            12,
            wallet_address="EQTestWalletAddress",
            telegram_id=900900,
        )

        self.assertEqual(post_mock.call_count, 1)
        kwargs = post_mock.call_args.kwargs
        self.assertEqual(kwargs["json"]["chat_id"], "@Tac_mining_withdrawal")
        text = kwargs["json"]["text"]
        self.assertIn("Withdrawal Under Review", text)
        self.assertIn("Cry***", text)
        self.assertNotIn("CryptoMiner", text)
        self.assertIn("🪪 ID: 900900", text)
        self.assertIn("4,600 TAC", text)
        self.assertIn("$5.00", text)
        self.assertIn("#12", text)
        self.assertIn("EQTestWa...ress", text)
        self.assertNotIn("Transaction Hash", text)
        self.assertNotIn("القاهرة", text)
        self.assertNotIn("parse_mode", kwargs["json"])
        self.assertNotIn("reply_markup", kwargs["json"])

    @patch("miner_app.views.requests.post")
    def test_proof_message_failure_is_logged_not_raised(self, post_mock):
        post_mock.side_effect = Exception("network down")

        with self.assertLogs("miner_app.views", level="ERROR"):
            send_withdrawal_proof_notification(
                "CryptoMiner", Decimal("4600"), Decimal("5"), 12
            )

    @patch("miner_app.views.requests.post")
    def test_proof_message_is_skipped_without_group_setting(self, post_mock):
        with override_settings(TELEGRAM_WITHDRAWAL_GROUP_ID=""):
            send_withdrawal_proof_notification(
                "CryptoMiner", Decimal("4600"), Decimal("5"), 12
            )

        post_mock.assert_not_called()

    @patch("miner_app.views.send_withdrawal_proof_notification")
    @patch("miner_app.views.send_admin_notification")
    def test_request_withdraw_sends_proof_with_tac_and_usdt(
        self, notification_mock, proof_mock
    ):
        user = User.objects.create(
            telegram_id=6101,
            username="CryptoMiner",
            wallet_address="EQTestWalletAddress",
            is_wallet_verified=True,
            usdt_balance=Decimal("5"),
        )
        for offset in range(3):
            User.objects.create(
                telegram_id=6102 + offset,
                username=f"friend{offset}",
                referred_by=user,
            )

        response = self.post_json(
            "/api/request-withdraw/",
            {"initData": build_init_data(6101), "amount": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(proof_mock.call_count, 1)
        (
            username,
            tac_amount,
            usdt_amount,
            request_id,
            wallet_address,
            status,
            telegram_id,
        ) = proof_mock.call_args.args
        self.assertEqual(username, "CryptoMiner")
        self.assertEqual(tac_amount, Decimal("4600.00000000"))
        self.assertEqual(usdt_amount, Decimal("5"))
        self.assertEqual(request_id, WithdrawRequest.objects.get(user=user).pk)
        self.assertEqual(wallet_address, "EQTestWalletAddress")
        self.assertEqual(status, "pending")
        self.assertEqual(telegram_id, 6101)

    def create_pending_withdrawal(self, telegram_id, amount=Decimal("5")):
        user = User.objects.create(
            telegram_id=telegram_id,
            username="CryptoMiner",
            wallet_address="EQTestWalletAddress",
            is_wallet_verified=True,
        )
        withdrawal = WithdrawRequest.objects.create(
            user=user,
            amount=amount,
            wallet_address="EQTestWalletAddress",
            status="pending",
        )
        return user, withdrawal

    @patch("miner_app.views.requests.post")
    def test_paid_proof_message_has_paid_status_and_no_buttons(self, post_mock):
        post_mock.return_value = FakeTelegramResponse(True)

        send_withdrawal_proof_notification(
            "CryptoMiner",
            Decimal("4600"),
            Decimal("5"),
            12,
            wallet_address="EQTestWalletAddress",
            status="completed",
            telegram_id=900900,
        )

        kwargs = post_mock.call_args.kwargs
        text = kwargs["json"]["text"]
        self.assertIn("Withdrawal Successful", text)
        self.assertNotIn("Under Review", text)
        self.assertNotIn("القاهرة", text)
        self.assertNotIn("reply_markup", kwargs["json"])

    @patch("miner_app.views.send_withdrawal_proof_notification")
    def test_marking_paid_posts_second_proof_message(self, proof_mock):
        _, withdrawal = self.create_pending_withdrawal(6201)

        withdrawal.status = "completed"
        withdrawal.save(update_fields=["status"])

        proof_mock.assert_called_once()
        (
            username,
            tac_amount,
            usdt_amount,
            request_id,
            wallet_address,
            status,
            telegram_id,
        ) = proof_mock.call_args.args
        self.assertEqual(username, "CryptoMiner")
        self.assertEqual(tac_amount, Decimal("4600.00000000"))
        self.assertEqual(usdt_amount, Decimal("5"))
        self.assertEqual(request_id, withdrawal.pk)
        self.assertEqual(wallet_address, "EQTestWalletAddress")
        self.assertEqual(status, "completed")
        self.assertEqual(telegram_id, 6201)

    @patch("miner_app.views.send_withdrawal_proof_notification")
    def test_completed_withdrawal_is_not_announced_twice(self, proof_mock):
        _, withdrawal = self.create_pending_withdrawal(6202)
        withdrawal.status = "completed"
        withdrawal.save(update_fields=["status"])

        withdrawal.note = "مرجع الدفع: TX123"
        withdrawal.save(update_fields=["note"])

        proof_mock.assert_called_once()

    @patch("miner_app.views.send_withdrawal_proof_notification")
    def test_rejected_withdrawal_is_not_announced(self, proof_mock):
        _, withdrawal = self.create_pending_withdrawal(6203)

        withdrawal.status = "rejected"
        withdrawal.save(update_fields=["status"])

        proof_mock.assert_not_called()


class HealthCheckTests(TestCase):
    def test_healthz_reports_ok(self):
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    WEBAPP_URL="https://tacminer.example.com",
)
class SetTelegramWebhookCommandTests(TestCase):
    @patch("miner_app.management.commands.set_telegram_webhook.requests.post")
    def test_registers_webhook_for_configured_domain(self, post_mock):
        post_mock.return_value.json.return_value = {"ok": True}

        call_command("set_telegram_webhook")

        url = post_mock.call_args.args[0]
        payload = post_mock.call_args.kwargs["json"]
        self.assertEqual(url, f"https://api.telegram.org/bot{TEST_BOT_TOKEN}/setWebhook")
        self.assertEqual(
            payload["url"], "https://tacminer.example.com/api/telegram-webhook/"
        )
        self.assertEqual(payload["secret_token"], TEST_WEBHOOK_SECRET)
        self.assertEqual(
            payload["allowed_updates"],
            ["message", "edited_message"],
        )

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_requires_bot_token(self):
        with self.assertRaises(CommandError):
            call_command("set_telegram_webhook")


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    ADSGRAM_BLOCK_ID="block-test",
    ADSGRAM_REWARD_SECRET="ads-secret",
    ADSGRAM_SESSION_TTL_SECONDS=600,
)
class AdsgramWorkerGateTests(TestCase):
    def setUp(self):
        self.worker = Worker.objects.create(
            name="Ad Worker",
            icon="fa-hammer",
            image="/static/images/1.png",
            base_price=Decimal("100"),
        )
        self.user = User.objects.create(
            telegram_id=8801,
            username="ad-user",
            balance=Decimal("1000"),
        )
        self.init_data = build_init_data(8801)

    def post_json(self, path, payload):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def start_session(self):
        response = self.post_json(
            "/api/ads/start/",
            {"initData": self.init_data, "worker_id": self.worker.id},
        )
        self.assertEqual(response.status_code, 200)
        return response

    def confirm_session(self, telegram_id=8801, secret="ads-secret"):
        return self.client.get(
            "/api/adsgram/postback/",
            {"user_id": str(telegram_id), "secret": secret},
        )

    def upgrade(self, token=None):
        return self.post_json(
            "/api/upgrade/",
            {
                "initData": self.init_data,
                "worker_id": self.worker.id,
                "ad_session": token,
            },
        )

    def test_start_creates_pending_session(self):
        response = self.start_session()

        data = response.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["block_id"], "block-test")
        session = AdSession.objects.get(token=data["session"])
        self.assertEqual(session.user, self.user)
        self.assertEqual(session.context_id, self.worker.id)
        self.assertEqual(session.status, AdSession.STATUS_PENDING)

    def test_upgrade_requires_ad_session(self):
        response = self.upgrade()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "ad_required")
        self.assertFalse(UserWorker.objects.filter(user=self.user).exists())

    def test_upgrade_waits_for_postback_confirmation(self):
        token = self.start_session().json()["session"]

        response = self.upgrade(token)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "ad_pending")

    def test_postback_confirms_and_upgrade_consumes_session(self):
        token = self.start_session().json()["session"]

        postback = self.confirm_session()
        self.assertEqual(postback.status_code, 200)
        self.assertEqual(postback.json(), {"ok": True})

        response = self.upgrade(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Upgraded!")
        self.assertEqual(response.json()["new_level"], 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.balance, Decimal("900.00000000"))
        session = AdSession.objects.get(token=token)
        self.assertEqual(session.status, AdSession.STATUS_CONSUMED)
        self.assertIsNotNone(session.consumed_at)

    def test_postback_rejects_invalid_secret(self):
        token = self.start_session().json()["session"]

        response = self.confirm_session(secret="wrong-secret")

        self.assertEqual(response.status_code, 403)
        session = AdSession.objects.get(token=token)
        self.assertEqual(session.status, AdSession.STATUS_PENDING)

    def test_session_is_bound_to_the_same_worker(self):
        token = self.start_session().json()["session"]
        self.confirm_session()
        other_worker = Worker.objects.create(
            name="Other Worker",
            icon="fa-cog",
            image="/static/images/2.png",
            base_price=Decimal("200"),
        )

        response = self.post_json(
            "/api/upgrade/",
            {
                "initData": self.init_data,
                "worker_id": other_worker.id,
                "ad_session": token,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "ad_required")

    def test_consumed_session_cannot_be_reused(self):
        token = self.start_session().json()["session"]
        self.confirm_session()
        self.assertEqual(self.upgrade(token).status_code, 200)

        response = self.upgrade(token)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "ad_required")

    @override_settings(ADSGRAM_BLOCK_ID="")
    def test_gate_is_disabled_without_block_id(self):
        response = self.upgrade()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Upgraded!")
        self.assertFalse(AdSession.objects.exists())


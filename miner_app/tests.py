import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from miner_app.models import (
    PendingReferral,
    User,
    UserTask,
    UserWorker,
    Worker,
    WithdrawRequest,
)
from miner_app.views import complete_withdraw_request, reject_withdraw_request


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


class ImmediateThread:
    """بديل للـ Thread في الاختبارات عشان يتنفذ فوراً."""

    def __init__(self, target=None, args=(), daemon=None, **kwargs):
        self._target = target
        self._args = args

    def start(self):
        if self._target:
            self._target(*self._args)


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

    @patch("miner_app.views.send_admin_notification")
    def test_withdrawals_are_allowed_multiple_times_per_day(self, notification_mock):
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

    @patch("miner_app.views.threading.Thread", ImmediateThread)
    @patch("miner_app.views.send_public_announcement")
    def test_complete_announces_only_after_paid_and_once(self, announce_mock):
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


@override_settings(
    TELEGRAM_BOT_TOKEN=TEST_BOT_TOKEN,
    TELEGRAM_WEBHOOK_SECRET=TEST_WEBHOOK_SECRET,
    TELEGRAM_INITDATA_MAX_AGE_SECONDS=86400,
    SECURE_SSL_REDIRECT=False,
    ALLOWED_HOSTS=["testserver"],
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

    @patch("miner_app.views.threading.Thread", ImmediateThread)
    @patch("miner_app.views.send_public_announcement")
    def test_admin_marks_withdrawal_paid(self, announce_mock):
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

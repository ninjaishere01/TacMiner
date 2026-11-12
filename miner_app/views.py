from django.shortcuts import render
from django.db import connection, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from .models import User, Worker, UserWorker, UserTask, WithdrawRequest, PendingReferral, AdSession 
from rest_framework.permissions import AllowAny
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
import requests
import random
import secrets
from django.conf import settings
from miner_app import tasks as task_queue
import time
import hmac
import hashlib
import json
import logging
import os
from urllib.parse import parse_qsl


logger = logging.getLogger(__name__)

WITHDRAW_TAC_PER_USDT = Decimal("1000")
MIN_WITHDRAW_USDT = Decimal("3")
DAILY_CHECKIN_REWARD = Decimal("10")

# حد أقصى لحجم initData عشان نمنع أي payload ضخم
TELEGRAM_INIT_DATA_MAX_LENGTH = 8192
# سماحية فرق الساعة بين سيرفر تيليجرام والسيرفر بتاعنا
TELEGRAM_AUTH_DATE_FUTURE_TOLERANCE = 60


# مكافآت دعوة الأصدقاء: اسم المهمة في UserTask -> (عدد الأصدقاء، المكافأة)
MILESTONE_REWARDS = {
    "milestone_3": (3, Decimal("30")),
    "milestone_5": (5, Decimal("50")),
    "milestone_10": (10, Decimal("100")),
    "milestone_20": (20, Decimal("200")),
}

# شريك: 20 مدعو، وكلهم لازم يكونوا فعّلوا التعدين مرة واحدة على الأقل
PARTNER_MILESTONE = "milestone_20"

CHANNEL_TASK_NAMES = (
    "channel_1",
    "channel_2",
    "channel_3",
    "channel_4",
    "channel_5",
    "channel_6",
)

CHANNEL_TASK_REWARDS = {
    "channel_1": Decimal("25"),
    "channel_2": Decimal("25"),
    "channel_3": Decimal("25"),
    "channel_4": Decimal("25"),
    "channel_5": Decimal("150"),
    "channel_6": Decimal("150"),
}

ALLOWED_TASK_NAMES = set(CHANNEL_TASK_NAMES) | set(MILESTONE_REWARDS)


def verify_telegram_data(init_data):
    """يتحقق من توقيع initData وحداثته ويرجع telegram_id، أو None لو مش صالح."""
    if not init_data or not settings.TELEGRAM_BOT_TOKEN:
        return None
    if len(str(init_data)) > TELEGRAM_INIT_DATA_MAX_LENGTH:
        return None
    try:
        parsed = dict(parse_qsl(str(init_data), keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
        secret_key = hmac.new(
            b"WebAppData",
            settings.TELEGRAM_BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        # التحقق من عمر initData: لازم يكون لسه جديد ومش من المستقبل
        auth_date_value = parsed.get("auth_date")
        if auth_date_value is None:
            return None
        try:
            auth_date = int(auth_date_value)
        except (TypeError, ValueError):
            return None

        max_age = int(
            getattr(settings, "TELEGRAM_INITDATA_MAX_AGE_SECONDS", 86400)
        )
        now_ts = int(time.time())
        if auth_date > now_ts + TELEGRAM_AUTH_DATE_FUTURE_TOLERANCE:
            return None
        if now_ts - auth_date > max_age:
            return None

        user_payload = json.loads(parsed.get("user") or "{}")
        telegram_id = user_payload.get("id")
        if telegram_id is None:
            return None
        return int(telegram_id)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def parse_referrer_telegram_id(referral_id):
    """بيرجع Telegram ID لصاحب الدعوة لو القيمة بالشكل ref_<id> أو رقم فقط."""
    if referral_id in (None, ""):
        return None
    referrer_text = str(referral_id).strip()
    if referrer_text.lower().startswith("ref_"):
        referrer_text = referrer_text[4:].strip()
    if not referrer_text.isdigit():
        return None
    return int(referrer_text)


def link_user_to_referrer(user, referrer_id):
    """يربط المستخدم بصاحب الدعوة. لو صاحب الدعوة لسه مش في الداتابيز، الرقم يتسجل في pending_referral."""
    if not referrer_id or referrer_id == user.telegram_id or user.referred_by_id:
        return False

    referrer = User.objects.filter(telegram_id=referrer_id).exclude(pk=user.pk).first()
    if referrer:
        user.referred_by = referrer
        user.pending_referral = None
        user.save(update_fields=["referred_by", "pending_referral"])
        return True

    if user.pending_referral != referrer_id:
        user.pending_referral = referrer_id
        user.save(update_fields=["pending_referral"])
    return False


def get_claimed_milestones(user):
    """أرقام مكافآت الدعوة اللي المستخدم استلمها فعلاً من جدول UserTask."""
    names = UserTask.objects.filter(
        user=user,
        task_name__in=MILESTONE_REWARDS.keys(),
    ).values_list("task_name", flat=True)
    claimed = []
    for name in names:
        suffix = str(name).split("_", 1)[-1]
        if suffix.isdigit():
            claimed.append(int(suffix))
    return sorted(set(claimed))




#  لحساب ارباح اليوزر من العمال 
def worker_hourly_rate(worker):
    """إنتاج العامل/ساعة عند المستوى 0. لو base_rate متسجل بنستخدمه،
    وإلا بنرجع للقاعدة القديمة (1% من السعر) للعمال اللي اتضافوا يدويًا."""
    if worker.base_rate is not None:
        return float(worker.base_rate)
    return float(worker.base_price) * 0.01


def calculate_user_production(user):
    user_workers = UserWorker.objects.filter(user=user).select_related('worker')
    
    total_per_hour = 0
    for uw in user_workers:
        worker_production = worker_hourly_rate(uw.worker) * (1.1 ** uw.level)
        total_per_hour += worker_production
        
    # التحقق من تفعيل البوست
    now = timezone.now()
    multiplier = 1.0
    
    if user.last_boost_time:
        # لو عدت ساعة من وقت تفعيل البوست، البوست مش شغال
        if now > user.last_boost_time + timedelta(hours=1):
            multiplier = 1.0
        else:
            multiplier = float(user.boost_multiplier)
            
    total_per_hour *= multiplier
    
    return {
        "per_hour": total_per_hour,
        "per_day": total_per_hour * 24,
        "per_month": total_per_hour * 24 * 30
    }


def calculate_user_production_per_hour(user):
    """نفس معادلة الإنتاج لكن بترجع Decimal عشان حسابات الرصيد تبقى دقيقة."""
    return Decimal(str(calculate_user_production(user)["per_hour"]))


ACTIVATION_WINDOW = timedelta(hours=24)


def get_activation_expiry(user):
    """نهاية نافذة التعدين الحالية (24 ساعة من آخر تفعيل) أو None."""
    if not user.last_activation_at:
        return None
    return user.last_activation_at + ACTIVATION_WINDOW


def is_mining_active(user, now=None):
    now = now or timezone.now()
    expiry = get_activation_expiry(user)
    return bool(expiry and now < expiry and user.ads_watched_today >= 3)


def get_activation_time_left(user, now=None):
    now = now or timezone.now()
    expiry = get_activation_expiry(user)
    if not expiry or now >= expiry:
        return 0
    return int((expiry - now).total_seconds())


def ensure_legacy_activation(user, now=None):
    """يوزرز كانوا مفعّلين قبل إضافة التوقيت: نبدأ لهم نافذة 24 ساعة من الآن."""
    now = now or timezone.now()
    if (
        user.last_activation_at is None
        and user.ads_watched_today >= 3
        and user.last_activation_date == now.date()
    ):
        user.last_activation_at = now
        user.save(update_fields=["last_activation_at"])
        return True
    return False


def effective_activation_ads_count(user, now=None):
    """عدد إعلانات التفعيل المعروض للفرونت: 3 لو التعدين شغال، 0 لو النافذة خلصت."""
    now = now or timezone.now()
    expiry = get_activation_expiry(user)
    if expiry is None:
        if user.last_activation_date == now.date():
            return user.ads_watched_today
        return 0
    return 3 if now < expiry else 0


def calculate_pending_earnings(user, now=None):
    """أرباح الفترة اللي فاتت من غير ما نلمس الداتابيز، ومحدودة بنهاية نافذة التفعيل."""
    now = now or timezone.now()
    if not user.last_collected_at:
        return Decimal("0")
    expiry = get_activation_expiry(user)
    if expiry is None:
        return Decimal("0")
    end = min(now, expiry)
    elapsed_seconds = (end - user.last_collected_at).total_seconds()
    if elapsed_seconds <= 0:
        return Decimal("0")
    per_second = calculate_user_production_per_hour(user) / Decimal("3600")
    earned = per_second * Decimal(str(elapsed_seconds))
    return earned if earned > 0 else Decimal("0")


def settle_earnings(user, now=None):
    """
    يقفل أرباح الفترة اللي فاتت بالسرعة الحالية قبل أي ترقية أو بوست.
    لازم يتنادى والمستخدم معمول له select_for_update جوه transaction.
    الأرباح محدودة بنهاية نافذة التفعيل عشان فترة التوقف ما تحسبش.
    """
    now = now or timezone.now()
    if get_activation_expiry(user) is None:
        return Decimal("0")
    earned = calculate_pending_earnings(user, now)
    user.last_collected_at = now
    if earned > 0:
        user.balance += earned
        if user.referred_by_id:
            referrer = User.objects.select_for_update().get(pk=user.referred_by_id)
            referrer.referral_balance += earned * Decimal("0.10")
            referrer.save(update_fields=["referral_balance"])
    user.save(update_fields=["balance", "last_collected_at"])
    return earned




@api_view(['POST'])
@permission_classes([AllowAny])
def login_or_register(request):
    telegram_id = verify_telegram_data(request.data.get('initData'))
    username = request.data.get('username', '')
    referral_id = request.data.get('referral_id')

    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    # بنشوف المستخدم موجود ولا جديد
    user, created = User.objects.get_or_create(
        telegram_id=telegram_id,
        defaults={'username': username}
    )

    now = timezone.now()
    ensure_legacy_activation(user, now)

    offline_earnings = 0
    if user.last_collected_at:
        elapsed_seconds = (timezone.now() - user.last_collected_at).total_seconds()
        if elapsed_seconds > 60:  # لو عدت دقيقة على الأقل
            earned = calculate_pending_earnings(user)
            if earned > 0:
                offline_earnings = float(earned)

    # ساعة الإنتاج تتسجل أول مرة بس. التجميع هو اللي يحرّك last_collected_at
    # عشان الرصيد المتراكم ما يتمسحش قبل ما السيرفر يحسبه.
    if user.last_collected_at is None:
        user.last_collected_at = timezone.now()
        user.save(update_fields=['last_collected_at'])

    # 3. تحديث اسم اليوزر
    display_name = (username or "").strip()
    if display_name and display_name.lower() not in {"newuser", "new user"}:
        if user.username != display_name:
            user.username = display_name
            user.save(update_fields=["username"])

    # 4. الإحالة الأساسية جاية من الـ Webhook ومتخزنة في PendingReferral
    pending = PendingReferral.objects.filter(telegram_id=telegram_id).first()
    if pending is not None:
        if (
            user.referred_by_id is None
            and pending.referral_id
            and pending.referral_id != user.telegram_id
        ):
            referrer = User.objects.filter(
                telegram_id=pending.referral_id
            ).exclude(pk=user.pk).first()
            if referrer:
                user.referred_by = referrer
                user.save(update_fields=["referred_by"])
        if user.referred_by_id is not None or pending.referral_id == user.telegram_id:
            pending.delete()

    # 5. لو start_param وصل من اللعبة نستخدمه طالما اليوزر لسه مش مربوط
    referrer_id = parse_referrer_telegram_id(referral_id)
    if not referrer_id and user.pending_referral:
        referrer_id = int(user.pending_referral)

    if referrer_id and (created or user.referred_by_id is None):
        link_user_to_referrer(user, referrer_id)

    # 6. بنجيب العمال اللي معاه
    user_workers = {}
    for uw in user.workers.all():
        user_workers[uw.worker.id] = uw.level

    # 7. بنحسب الوقت المتبقي للمضاعفة (ساعة واحدة)
        # 7. بنحسب الوقت المتبقي للمضاعفة (ساعة) + الاستراحة (8 ساعات)
    time_left = 0
    boost_cooldown_left = 0  # <-- جديد
    if user.last_boost_time:
        boost_end_time = user.last_boost_time + timedelta(hours=1)
        now = timezone.now()
        if now < boost_end_time:
            # البوست شغال (ساعة)
            time_left = int((boost_end_time - now).total_seconds())
        else:
            # البوست خلص، بنشوف لو في استراحة 8 ساعات
            if user.boost_multiplier != 1.0:
                user.boost_multiplier = 1.0
                user.save(update_fields=["boost_multiplier"])

            cooldown_end = user.last_boost_time + timedelta(hours=8)
            if now < cooldown_end:
                boost_cooldown_left = int((cooldown_end - now).total_seconds())
            elif user.boost_ads_watched >= 5:
                # الاستراحة خلصت: نجهز دورة جديدة
                user.boost_ads_watched = 0
                user.save(update_fields=["boost_ads_watched"])
    elif user.boost_ads_watched >= 5:
        # حالة قديمة علقت عند 5 إعلانات من غير تفعيل: نعيد ضبط العداد
        user.boost_ads_watched = 0
        user.save(update_fields=["boost_ads_watched"])

    # 8. بنحسب الإنتاج الحالي للعمال
    production = calculate_user_production(user)
    referred_users = list(user.referrals.all())
    referrals_list = []
    active_referrals_count = 0
    for ref in referred_users:
        is_active = ref.last_activation_at is not None
        active_referrals_count += 1 if is_active else 0
        referrals_list.append({
            "username": ref.username or str(ref.telegram_id),
            "balance": float(ref.balance),
            "active": is_active,
        })
    referrals_count = len(referrals_list)

    return Response({
        "message": "Success",
        "user": {
            "telegram_id": user.telegram_id,
            "username": user.username,
            "balance": float(user.balance),
            "usdt_balance": float(user.usdt_balance),
            "referral_balance": float(user.referral_balance),
            "workers": user_workers,
            "is_partner": user.is_partner,
            "is_wallet_verified": user.is_wallet_verified,
            "wallet_address": user.wallet_address,
            "ads_watched_today": effective_activation_ads_count(user, now),
            "mining_active": is_mining_active(user, now),
            "activation_time_left": get_activation_time_left(user, now),
            "boost_ads_watched": user.boost_ads_watched,
            "last_boost_time": user.last_boost_time.isoformat() if user.last_boost_time else None,
            "boost_multiplier": float(user.boost_multiplier),
            "time_left": time_left,
            "referrals": referrals_list,
            "referrals_count": referrals_count,
            "active_referrals_count": active_referrals_count,
            "claimed_milestones": get_claimed_milestones(user),
            "production": production,
            "completed_tasks": [task.task_name for task in user.tasks.all()],
            "offline_earnings": offline_earnings,
            "has_checked_today": user.last_daily_check == timezone.now().date() if user.last_daily_check else False,
            "boost_cooldown_left": boost_cooldown_left,
            "daily_streak": user.daily_streak,
        }
    })





@api_view(['POST'])
@permission_classes([AllowAny])
def collect_gold(request):
    telegram_id = verify_telegram_data(request.data.get('initData'))
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            now = timezone.now()
            ensure_legacy_activation(user, now)

            if not is_mining_active(user, now):
                expiry = get_activation_expiry(user)
                if expiry and now >= expiry:
                    return Response({
                        "error": "انتهى تفعيل التعدين. شاهد 3 إعلانات لتفعيله 24 ساعة من جديد"
                    }, status=400)
                return Response({
                    "error": "يجب مشاهدة 3 إعلانات لتفعيل التعدين"
                }, status=400)

            earned = settle_earnings(user, now)

        return Response({
            "message": "Collected!",
            "new_balance": float(user.balance),
            "collected": float(earned),
        })

    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception:
        logger.exception("collect_gold failed for telegram_id=%s", telegram_id)
        return Response({"error": "حدث خطأ غير متوقع، حاول مرة أخرى"}, status=500)
    



@api_view(['POST'])
@permission_classes([AllowAny])
def upgrade_worker(request):
    telegram_id = verify_telegram_data(request.data.get('initData'))
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    worker_id = request.data.get('worker_id')

    if not worker_id:
        return Response({"error": "Missing data"}, status=400)

    ad_session_token = request.data.get('ad_session')

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            worker = Worker.objects.get(id=worker_id)

            ad_session = None
            if _adsgram_enabled():
                now = timezone.now()
                ad_session = (
                    AdSession.objects.select_for_update()
                    .filter(
                        user=user,
                        token=str(ad_session_token or ''),
                        purpose=AdSession.PURPOSE_WORKER,
                        context_id=worker.id,
                    )
                    .first()
                )
                if ad_session is None or ad_session.expires_at <= now:
                    return Response({
                        "error": "ad_required",
                        "message": "لازم تشاهد الإعلان الأول قبل العملية.",
                    }, status=400)
                if ad_session.status == AdSession.STATUS_PENDING:
                    return Response({
                        "error": "ad_pending",
                        "message": "جاري تأكيد الإعلان، استنى لحظة.",
                    }, status=409)
                if ad_session.status != AdSession.STATUS_CONFIRMED:
                    return Response({
                        "error": "ad_required",
                        "message": "الإعلان مستخدم بالفعل. شاهده من جديد.",
                    }, status=400)

            user_worker, created = UserWorker.objects.select_for_update().get_or_create(
                user=user, worker=worker
            )

            # نقفل أرباح الفترة اللي فاتت بسرعة الترقية القديمة قبل ما نطقّي السرعه
            ensure_legacy_activation(user)
            settle_earnings(user, timezone.now())

            current_level = user_worker.level
            price = worker.base_price * (Decimal('1.5') ** current_level)

            if user.balance < price:
                return Response({"error": "Insufficient balance"}, status=400)

            user.balance -= price
            user_worker.level += 1
            user.save(update_fields=["balance"])
            user_worker.save(update_fields=["level"])

            if ad_session is not None:
                ad_session.status = AdSession.STATUS_CONSUMED
                ad_session.consumed_at = timezone.now()
                ad_session.save(update_fields=["status", "consumed_at"])

            # احسب الإنتاج الجديد (بعد الترقية)
            production = calculate_user_production(user)

        return Response({
            "message": "Upgraded!", 
            "new_balance": float(user.balance),
            "new_level": user_worker.level,
            "production": production 
        })
            
    except (User.DoesNotExist, Worker.DoesNotExist):
        return Response({"error": "User or Worker not found"}, status=404)





@api_view(['GET'])
@permission_classes([AllowAny])
def get_workers(request):
    workers = Worker.objects.all()
    data = []
    for w in workers:
        data.append({
            "id": w.id,
            "name": w.name,
            "icon": w.icon,
            "image": w.image,
            "basePrice": float(w.base_price) # بنرجع السعر كرقم عشان الجافاسكريبت
        })
    return Response(data)



@api_view(['POST'])
@permission_classes([AllowAny])
def claim_task_reward(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    task_name = request.data.get('task_name')

    if not task_name:
        return Response({"error": "Missing data"}, status=400)

    # بنرفض أي اسم مهمة مش معروف عشان محدش يقدر يطبع رصيد بأسماء عشوائية
    if task_name not in ALLOWED_TASK_NAMES:
        return Response({"error": "مهمة غير معروفة"}, status=400)

    channel_tasks = {
        "channel_1": settings.TELEGRAM_TASK_CHANNEL_1,
        "channel_2": settings.TELEGRAM_TASK_CHANNEL_2,
        "channel_3": settings.TELEGRAM_TASK_CHANNEL_3,
        "channel_4": settings.TELEGRAM_TASK_CHANNEL_4,
        "channel_5": getattr(settings, "TELEGRAM_TASK_CHANNEL_5", None),
        "channel_6": getattr(settings, "TELEGRAM_TASK_CHANNEL_6", None),
    }
    if task_name in channel_tasks and not telegram_user_in_channel(telegram_id, channel_tasks[task_name]):
        return Response({"error": "أنت لست عضوًا. انضم أولاً ثم اضغط تحقق"}, status=400)

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)

            if UserTask.objects.filter(user=user, task_name=task_name).exists():
                return Response({"error": "لقد قمت بإكمال هذه المهمة بالفعل!"}, status=400)

            # مكافآت الدعوة بتتسجل في UserTask وبتضاف لرصيد الإحالة
            if task_name in MILESTONE_REWARDS:
                required_friends, reward_amount = MILESTONE_REWARDS[task_name]
                if task_name == PARTNER_MILESTONE:
                    # شريك: لازم 20 صديق فعّلوا التعدين مرة واحدة على الأقل
                    friends_count = user.referrals.filter(
                        last_activation_at__isnull=False
                    ).count()
                    shortfall_message = (
                        "لازم 20 صديق فعّلوا التعدين مرة واحدة على الأقل"
                    )
                else:
                    friends_count = user.referrals.count()
                    shortfall_message = "عدد الأصدقاء المدعوين غير كافٍ"
                if friends_count < required_friends:
                    return Response({"error": shortfall_message}, status=400)

                user.referral_balance += reward_amount
                update_fields = ["referral_balance"]
                if task_name == PARTNER_MILESTONE:
                    user.is_partner = True
                    update_fields.append("is_partner")
                user.save(update_fields=update_fields)
                UserTask.objects.create(user=user, task_name=task_name)

                return Response({
                    "message": "Task Completed!",
                    "reward": float(reward_amount),
                    "referral_balance": float(user.referral_balance),
                    "new_balance": float(user.balance),
                    "claimed_milestones": get_claimed_milestones(user),
                    "referrals_count": user.referrals.count(),
                    "active_referrals_count": friends_count,
                    "is_partner": user.is_partner,
                })

            # مهام القنوات: المكافأة محددة من السيرفر لكل قناة
            reward_amount = CHANNEL_TASK_REWARDS.get(task_name)
            if reward_amount is None:
                return Response({"error": "مهمة غير معروفة"}, status=400)

            user.balance += reward_amount
            user.save(update_fields=["balance"])
            UserTask.objects.create(user=user, task_name=task_name)

            return Response({
                "message": "Task Completed!",
                "new_balance": float(user.balance),
                "reward": float(reward_amount)
            })
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


from tonsdk.utils import Address


@api_view(['POST'])
@permission_classes([AllowAny])
def verify_wallet(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    wallet_address = request.data.get('wallet_address') # العنوان اللي جاي من TON Connect

    if not wallet_address:
        return Response({"error": "Missing data"}, status=400)

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            if str(wallet_address).startswith("0:"):
                try:
                    from tonsdk.utils import Address
                    addr = Address(wallet_address)
                    friendly_addr = addr.to_string(is_user_friendly=True, is_bounceable=False, is_url_safe=True)
                    user.wallet_address = friendly_addr
                except Exception:
                    user.wallet_address = wallet_address
            else:
                user.wallet_address = wallet_address
            user.is_wallet_verified = True # بنوثق المحفظة
            user.save(update_fields=["wallet_address", "is_wallet_verified"])

        return Response({"message": "Wallet Verified!", "is_wallet_verified": True})
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


@api_view(['POST'])
@permission_classes([AllowAny])
def disconnect_wallet(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)
    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            user.wallet_address = None
            user.is_wallet_verified = False
            user.save(update_fields=["wallet_address", "is_wallet_verified"])
        return Response({"message": "Wallet Disconnected!"})
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)




@api_view(['POST'])
@permission_classes([AllowAny])
def watch_ad(request):
    telegram_id = verify_telegram_data(request.data.get('initData'))
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    ad_type = request.data.get('ad_type')

    if not ad_type:
        return Response({"error": "Missing data"}, status=400)

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            now = timezone.now()
            today = now.date()
            ensure_legacy_activation(user, now)

            # --- 1. إعلانات تفعيل التعدين (3 إعلانات = نافذة 24 ساعة) ---
            if ad_type == 'activation':
                # ربط المحفظة شرط لبدء التعدين (يقلل الحسابات المتعددة)
                if not (user.is_wallet_verified and user.wallet_address):
                    return Response({
                        "error": "لازم تربط محفظتك الأول من صفحة الحساب عشان تبدأ التعدين.",
                        "code": "wallet_required",
                    }, status=400)

                expiry = get_activation_expiry(user)
                if expiry and now < expiry:
                    time_left = int((expiry - now).total_seconds())
                    return Response({
                        "error": f"التعدين مفعّل بالفعل. باقي {time_left} ثانية.",
                        "activation_time_left": time_left,
                    }, status=400)

                if expiry:
                    # النافذة القديمة انتهت: نقفل أرباحها لحد وقت الانتهاء فقط
                    settle_earnings(user, now)

                # نبدأ دورة جديدة لو النافذة القديمة خلصت أو لو الإعلانات من يوم فات
                if expiry or user.last_activation_date != today:
                    user.ads_watched_today = 0

                user.last_activation_at = None
                user.last_activation_date = today
                user.ads_watched_today += 1

                if user.ads_watched_today >= 3:
                    user.last_activation_at = now
                    user.last_collected_at = now
                    user.save(update_fields=[
                        "ads_watched_today",
                        "last_activation_at",
                        "last_activation_date",
                        "last_collected_at",
                    ])
                else:
                    user.save(update_fields=[
                        "ads_watched_today",
                        "last_activation_at",
                        "last_activation_date",
                    ])

                activation_active = user.ads_watched_today >= 3
                return Response({
                    "message": "Ad Watched!",
                    "new_balance": float(user.balance),
                    "watched_count": user.ads_watched_today,
                    "reward": 0.0,
                    "mining_active": activation_active,
                    "activation_time_left": int(ACTIVATION_WINDOW.total_seconds()) if activation_active else 0,
                })

            # --- 2. إعلانات المضاعفة (Boost) ---
            elif ad_type == 'boost':
                # 1. لو البوست شغال حالياً (يعني الساعة لسه مخلصتش)
                if user.last_boost_time and user.boost_multiplier > 1.0:
                    boost_end_time = user.last_boost_time + timedelta(hours=1)
                    if now < boost_end_time:
                        return Response({"error": "البوست شغال بالفعل. استنى لما يخلص."}, status=400)
                    # لو خلصت الساعة، بنلغي البوست ونرجع السرعة الطبيعية
                    user.boost_multiplier = 1.0
                    user.save(update_fields=["boost_multiplier"])

                # 2. لو خلص دورة الـ 5 إعلانات: لازم 8 ساعات من وقت تفعيل البوست
                if user.boost_ads_watched >= 5:
                    if user.last_boost_time:
                        cooldown_end = user.last_boost_time + timedelta(hours=8)
                        if now < cooldown_end:
                            time_left_cooldown = int((cooldown_end - now).total_seconds())
                            return Response({"error": f"لازم تستنى {time_left_cooldown} ثانية."}, status=400)
                    # الاستراحة خلصت (أو حالة قديمة): نبدأ دورة جديدة
                    user.boost_ads_watched = 0
                    user.save(update_fields=["boost_ads_watched"])

                # 3. نسجل الإعلان، والخامس بيفعّل المضاعف فوراً
                user.boost_ads_watched += 1

                if user.boost_ads_watched >= 5:
                    # نقفل أرباح الفترة اللي فاتت قبل ما المضاعف يتفعّل
                    settle_earnings(user, now)
                    user.boost_multiplier = Decimal('2.00')
                    user.last_boost_time = now
                    user.save(update_fields=[
                        "boost_ads_watched",
                        "boost_multiplier",
                        "last_boost_time",
                    ])
                    return Response({
                        "message": "Boost Activated!",
                        "boost_multiplier": float(user.boost_multiplier),
                        "watched_count": user.boost_ads_watched,
                    })

                user.save(update_fields=["boost_ads_watched"])
                return Response({
                    "message": "Boost Ad Watched!",
                    "watched_count": user.boost_ads_watched
                })

            return Response({"error": "Invalid ad type"}, status=400)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



def _adsgram_enabled():
    return bool(settings.ADSGRAM_BLOCK_ID)


@api_view(['POST'])
@permission_classes([AllowAny])
def start_ad_session(request):
    """بتتفتح قبل عرض إعلان AdsGram وترجّع توكن الجلسة للواجهة."""
    telegram_id = verify_telegram_data(request.data.get('initData'))
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    if not _adsgram_enabled():
        return Response({"enabled": False})

    worker_id = request.data.get('worker_id')
    try:
        worker_id = int(worker_id)
    except (TypeError, ValueError):
        return Response({"error": "Missing data"}, status=400)

    if not Worker.objects.filter(id=worker_id).exists():
        return Response({"error": "Worker not found"}, status=404)

    try:
        user = User.objects.get(telegram_id=telegram_id)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)

    session = AdSession.objects.create(
        user=user,
        purpose=AdSession.PURPOSE_WORKER,
        context_id=worker_id,
        token=secrets.token_urlsafe(24),
        expires_at=timezone.now()
        + timedelta(seconds=settings.ADSGRAM_SESSION_TTL_SECONDS),
    )

    return Response({
        "enabled": True,
        "session": session.token,
        "block_id": settings.ADSGRAM_BLOCK_ID,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def adsgram_postback(request):
    """AdsGram بتنادي هنا بعد ما المستخدم يخلص الإعلان، وبتأكد الجلسة المفتوحة."""
    configured_secret = settings.ADSGRAM_REWARD_SECRET
    received_secret = str(request.query_params.get('secret') or '')
    if (
        not configured_secret
        or not hmac.compare_digest(received_secret, configured_secret)
    ):
        logger.warning("AdsGram postback rejected: invalid secret")
        return Response({"ok": False}, status=403)

    raw_user_id = (
        request.query_params.get('user_id')
        or request.query_params.get('userId')
    )
    try:
        telegram_id = int(raw_user_id)
    except (TypeError, ValueError):
        logger.warning("AdsGram postback rejected: invalid userId=%r", raw_user_id)
        return Response({"ok": False}, status=400)

    now = timezone.now()
    with transaction.atomic():
        try:
            user = User.objects.get(telegram_id=telegram_id)
        except User.DoesNotExist:
            # مستخدم غير معروف: نرجّع 200 عشان AdsGram ما تعيدش المحاولة.
            logger.warning("AdsGram postback for unknown telegram_id=%s", telegram_id)
            return Response({"ok": True})

        session = (
            AdSession.objects.select_for_update()
            .filter(
                user=user,
                status=AdSession.STATUS_PENDING,
                expires_at__gt=now,
            )
            .order_by('-created_at')
            .first()
        )
        if session is None:
            logger.warning(
                "AdsGram postback without open session for telegram_id=%s",
                telegram_id,
            )
            return Response({"ok": True})

        session.status = AdSession.STATUS_CONFIRMED
        session.confirmed_at = now
        session.save(update_fields=["status", "confirmed_at"])

    logger.info(
        "AdsGram confirmed ad session=%s user=%s", session.pk, telegram_id
    )
    return Response({"ok": True})


@api_view(['POST'])
@permission_classes([AllowAny])
def exchange_currency(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    tac_amount = request.data.get('tac_amount')

    if not tac_amount:
        return Response({"error": "Missing data"}, status=400)

    try:
        tac_amount = Decimal(str(tac_amount))
        if tac_amount < WITHDRAW_TAC_PER_USDT:
            return Response(
                {"error": f"الحد الأدنى {int(WITHDRAW_TAC_PER_USDT)} TAC"}, status=400
            )

        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)

            if user.balance < tac_amount:
                return Response({"error": "رصيدك غير كافي"}, status=400)

            # حساب الـ USDT (كل 1000 TAC = 1 USDT)
            usdt_added = tac_amount / WITHDRAW_TAC_PER_USDT

            # خصم الـ TAC وإضافة الـ USDT
            user.balance -= tac_amount
            user.usdt_balance += usdt_added
            user.save(update_fields=["balance", "usdt_balance"])

        return Response({
            "message": "Exchange Successful!",
            "new_tac_balance": float(user.balance),
            "new_usdt_balance": float(user.usdt_balance),
            "usdt_added": float(usdt_added)
        })

    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception:
        logger.exception("exchange_currency failed for telegram_id=%s", telegram_id)
        return Response({"error": "حدث خطأ غير متوقع، حاول مرة أخرى"}, status=500)


@api_view(['POST'])
@permission_classes([AllowAny])
def collect_referral_gold(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)
        
    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)

            # بنشوف لو فيه رصيد إحالة متاح
            if user.referral_balance <= 0:
                return Response({"error": "لا يوجد رصيد إحالة للسحب"}, status=400)

            amount_to_collect = user.referral_balance

            # بنضيف رصيد الإحالة للرصيد الأساسي
            user.balance += amount_to_collect
            user.referral_balance = 0 # بنصفر رصيد الإحالة
            user.save(update_fields=["balance", "referral_balance"])

        return Response({
            "message": "Referral Gold Collected!",
            "new_balance": float(user.balance),
            "collected_amount": float(amount_to_collect)
        })
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



def enqueue_task(task, *args, **kwargs):
    """يبعت المهمة للطابور؛ لو الطابور مش متاح ينفذها مباشرة."""
    try:
        task.apply_async(args=args, **kwargs)
    except Exception:
        logger.exception(
            "Failed to enqueue Celery task %s; running synchronously",
            getattr(task, "name", task),
        )
        task(*args)


@api_view(['POST'])
@permission_classes([AllowAny])
def request_withdraw(request):
    telegram_id = verify_telegram_data(request.data.get('initData'))
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    amount = request.data.get('amount')

    if not amount:
        return Response({"error": "Missing data"}, status=400)

    try:
        withdraw_amount = Decimal(str(amount))
        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)
            wallet_address = user.wallet_address

            if not wallet_address:
                return Response({"error": "لا توجد محفظة محفوظة"}, status=400)

            if withdraw_amount < MIN_WITHDRAW_USDT:
                return Response(
                    {"error": f"الحد الأدنى للسحب {MIN_WITHDRAW_USDT} USDT"},
                    status=400,
                )

            if user.usdt_balance < withdraw_amount:
                return Response({"error": "رصيدك غير كافي"}, status=400)
            if user.referrals.count() < 3:
                return Response({"error": "يجب عليك دعوة 3 أصدقاء على الأقل للسحب"}, status=400)

            user.usdt_balance -= withdraw_amount
            user.save(update_fields=["usdt_balance"])

            withdraw_request = WithdrawRequest.objects.create(
                user=user,
                amount=withdraw_amount,
                wallet_address=wallet_address,
                status='pending'
            )
            new_usdt_balance = float(user.usdt_balance)
            username = user.username
            tac_amount = (withdraw_amount * WITHDRAW_TAC_PER_USDT).quantize(
                Decimal("0.00000001")
            )

        enqueue_task(
            task_queue.send_admin_alert,
            username,
            float(withdraw_amount),
            wallet_address,
        )
        enqueue_task(
            task_queue.send_withdrawal_proof,
            username,
            tac_amount,
            withdraw_amount,
            withdraw_request.pk,
            wallet_address,
            "pending",
            telegram_id,
        )

        return Response({
            "message": "Withdraw Request Submitted!",
            "new_usdt_balance": new_usdt_balance
        })

    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception:
        logger.exception("request_withdraw failed for telegram_id=%s", telegram_id)
        return Response({"error": "حدث خطأ غير متوقع، حاول مرة أخرى"}, status=500)
    



@api_view(['POST'])
@permission_classes([AllowAny])
def get_withdraw_history(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)
        
    try:
        user = User.objects.get(telegram_id=telegram_id)
        withdrawals = user.withdrawals.all().order_by('-created_at')
        
        data = []
        for w in withdrawals:
            data.append({
                "id": w.id,
                "amount": float(w.amount),
                "wallet_address": w.wallet_address,
                "status": w.status,
                "note": w.note or "",
                "date": w.created_at.strftime("%Y-%m-%d %H:%M")
            })
            
        return Response(data)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



def _escape_markdown(value):
    """يهرب حروف Markdown عشان اسم المستخدم ما يبوظش رسالة تيليجرام."""
    text = str(value or "")
    for char in ("\\", "_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


def complete_withdraw_request(withdraw_request, payment_reference=""):
    """
    بيعلّم الطلب كمدفوع مرة واحدة بس، وبعد كده بيبعت إعلان القناة.
    العملية نفسها ذرّية؛ الإعلان بيتحاول يتنفذ بعد نجاح الحفظ.
    """
    with transaction.atomic():
        request_row = WithdrawRequest.objects.select_for_update().get(
            pk=withdraw_request.pk
        )
        if request_row.status != "pending":
            return False

        request_row.status = "completed"
        if payment_reference:
            request_row.note = f"مرجع الدفع: {payment_reference}"
        request_row.save(update_fields=["status", "note"])

        username = request_row.user.username
        amount = float(request_row.amount)
        wallet_address = request_row.wallet_address

    enqueue_task(
        task_queue.send_public_announcement,
        username,
        amount,
        wallet_address,
        countdown=random.randint(180, 600),
    )
    return True


def reject_withdraw_request(withdraw_request, reason=""):
    """بيرفض الطلب ويرجع قيمته بالـ TAC للمستخدم مرة واحدة بس جوه transaction."""
    with transaction.atomic():
        request_row = WithdrawRequest.objects.select_for_update().get(
            pk=withdraw_request.pk
        )
        if request_row.status != "pending":
            return False

        user = User.objects.select_for_update().get(pk=request_row.user_id)
        refund_tac = (request_row.amount * WITHDRAW_TAC_PER_USDT).quantize(
            Decimal("0.00000001")
        )
        user.balance += refund_tac
        user.save(update_fields=["balance"])

        request_row.status = "rejected"
        if reason:
            request_row.note = reason
        request_row.save(update_fields=["status", "note"])
    return True


def send_withdraw_announcement(username, amount, wallet_address):
    """بتبعت رسالة في القناة بعد وقت عشوائي (من 3 لـ 10 دقايق)"""
    # تأخير عشوائي
    delay = random.randint(180, 600) # من 180 ثانية لـ 600 ثانية
    time.sleep(delay)

    # تجهيز الرسالة
    message = f"""
🎉 **تم صرف أرباح جديدة!** 🎉

👤 **المستخدم:** {username}
💰 **المبلغ:** {amount:.2f} USDT
💳 **المحفظة:** `{wallet_address[:10]}...{wallet_address[-5:]}`

🚀 **كن أنت التالي!** العب واجمع وسحب أرباحك الآن.
    """

    # إرسال الرسالة لتيليجرام
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")



# 1. دالة إرسال إشعار لقناة الأدمن (فوراً)
def send_admin_notification(username, amount, wallet_address):
    message = f"""
🔔 **طلب سحب جديد!**

👤 **المستخدم:** {_escape_markdown(username)}
💰 **المبلغ:** {amount:.2f} USDT
💳 **المحفظة:** `{_escape_markdown(wallet_address)}`

⏳ **الحالة:** قيد المراجعة
    """
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_ADMIN_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        logger.exception("Failed to send admin withdrawal notification")





def mask_username(username):
    """يخفي جزء من الاسم للخصوصية: أول 3 حروف + ***."""
    name = (username or "").strip()
    if not name:
        return "Anonymous"
    return f"{name[:3]}***"


def _mask_wallet(wallet_address):
    value = (wallet_address or "").strip()
    if not value:
        return "—"
    if len(value) <= 12:
        return value
    return f"{value[:8]}...{value[-4:]}"


WITHDRAWAL_PROOF_HEADERS = {
    "pending": "Withdrawal Under Review",
    "completed": "Withdrawal Successful",
    "rejected": "Withdrawal Rejected",
}


def _withdrawal_proof_text(
    username,
    tac_amount,
    usdt_amount,
    request_id,
    wallet_address="",
    status="pending",
    telegram_id="",
):
    tac_display = f"{tac_amount:,.2f}".rstrip("0").rstrip(".")
    usdt_display = f"{usdt_amount:,.2f}"
    header = WITHDRAWAL_PROOF_HEADERS.get(status, "Withdrawal")

    lines = [
        header,
        f"#{request_id}",
        "",
        f"👤 ⌛ {mask_username(username)}",
        "",
    ]
    if telegram_id not in (None, ""):
        lines += [f"🪪 ID: {telegram_id}", ""]
    lines += [
        "💎 Amount:",
        f"{tac_display} TAC",
        "",
        "💵 Coins Deducted:",
        f"${usdt_display}",
        "",
        "🔐 Wallet Address:",
        _mask_wallet(wallet_address),
    ]
    return "\n".join(lines)


def send_withdrawal_proof_notification(
    username,
    tac_amount,
    usdt_amount,
    request_id,
    wallet_address="",
    status="pending",
    telegram_id="",
):
    """إشعار سحب في قناة السحوبات (قيد المراجعة أو مدفوعة)."""
    chat_id = getattr(settings, "TELEGRAM_WITHDRAWAL_GROUP_ID", "")
    if not chat_id or not settings.TELEGRAM_BOT_TOKEN:
        return

    message = _withdrawal_proof_text(
        username,
        tac_amount,
        usdt_amount,
        request_id,
        wallet_address=wallet_address,
        status=status,
        telegram_id=telegram_id,
    )
    payload = {
        "chat_id": chat_id,
        "text": message,
    }

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json=payload, timeout=10)
        if not response.json().get("ok"):
            logger.warning("Telegram withdrawal proof message returned not ok")
    except Exception:
        logger.exception("Failed to send withdrawal proof notification")


# 2. دالة إرسال إعلان لقناة الجمهور (بتتنادى بعد ما الأدمن يعلّم الطلب كمدفوع)
def send_public_announcement(username, amount, wallet_address):
    message = f"""
🎉 **تم صرف أرباح جديدة!** 🎉

👤 **المستخدم:** {_escape_markdown(username)}
💰 **المبلغ:** {amount:.2f} USDT
💳 **المحفظة:** `{_escape_markdown(wallet_address[:10])}...{_escape_markdown(wallet_address[-5:])}`

🚀 **كن أنت التالي!** العب واجمع وسحب أرباحك الآن.
    """
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        logger.exception("Failed to send public withdrawal announcement")








WELCOME_CAPTION = (
    "💎 TAC MINING 💎\n\n"
    "Your mining crew is ready!\n\n"
    "⛏ Mine TAC every day\n"
    "⚒ Upgrade your workers to earn more\n"
    "🎁 Complete tasks & claim daily rewards\n"
    "💸 Withdraw safely at 3000 TAC ($3)\n\n"
    "Start mining now 👇"
)


def _start_welcome_markup():
    webapp_url = getattr(settings, "WEBAPP_URL", "") or "https://tacminer.onrender.com"
    return {
        "inline_keyboard": [
            [{"text": "⛏ Start Mining", "web_app": {"url": webapp_url}}]
        ]
    }


def _send_start_welcome_text(chat_id, reply_markup):
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": WELCOME_CAPTION,
            "reply_markup": reply_markup,
        },
        timeout=15,
    )
    if not response.json().get("ok"):
        logger.warning("Telegram sendMessage for /start welcome returned not ok")


def send_start_welcome(chat_id):
    """يبعت ترحيب /start بصورة، ولو الصورة فشلت يبعت نفس النص. ما بيرفعش استثناء."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; skipping /start welcome")
        return

    reply_markup = _start_welcome_markup()
    photo_path = os.path.join(
        settings.BASE_DIR, "backend_core", "static", "images", "welcome.jpg"
    )

    try:
        if not os.path.isfile(photo_path):
            raise FileNotFoundError(photo_path)

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as photo:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": WELCOME_CAPTION,
                    "reply_markup": json.dumps(reply_markup),
                },
                files={"photo": ("welcome.jpg", photo, "image/jpeg")},
                timeout=20,
            )
        if not response.json().get("ok"):
            raise RuntimeError("Telegram sendPhoto returned not ok")
        return
    except Exception:
        logger.exception("Failed to send /start welcome photo; falling back to text")

    try:
        _send_start_welcome_text(chat_id, reply_markup)
    except Exception:
        logger.exception("Failed to send /start welcome text")


def _telegram_start_payload(text):
    """يطلع الـ payload من رسالة زي /start ref_123 أو /start@Bot ref_123."""
    parts = (text or "").strip().split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].lower()
    if command != "/start" or len(parts) < 2:
        return None
    return parts[1].strip()


@api_view(['POST'])
@permission_classes([AllowAny])
def telegram_webhook(request):
    """يستقبل POST من تيليجرام. /start ref_<id> يتخزن في PendingReferral ويرجع 200."""
    configured_secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", None)
    if configured_secret:
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(str(received_secret), str(configured_secret)):
            logger.warning("Rejected Telegram webhook request with invalid secret token")
            return Response({"ok": False}, status=403)
    else:
        logger.warning(
            "TELEGRAM_WEBHOOK_SECRET is not configured; webhook requests are unauthenticated"
        )

    try:
        message = request.data.get("message") or request.data.get("edited_message") or {}
    except Exception:
        logger.exception("telegram_webhook failed to read update")
        message = {}

    if isinstance(message, dict):
        try:
            payload = _telegram_start_payload(message.get("text") or "")
            from_user = message.get("from") or {}
            raw_telegram_id = from_user.get("id") if isinstance(from_user, dict) else None
            referrer_id = parse_referrer_telegram_id(payload)
            if raw_telegram_id not in (None, "") and referrer_id:
                telegram_id = int(raw_telegram_id)
                if referrer_id != telegram_id:
                    PendingReferral.objects.update_or_create(
                        telegram_id=telegram_id,
                        defaults={"referral_id": referrer_id},
                    )
        except Exception:
            logger.exception("telegram_webhook failed to process update")

    try:
        start_message = request.data.get("message")
        if isinstance(start_message, dict):
            chat = start_message.get("chat") or {}
            text = (start_message.get("text") or "").strip()
            parts = text.split()
            command = parts[0].split("@", 1)[0].lower() if parts else ""
            if (
                command == "/start"
                and isinstance(chat, dict)
                and chat.get("type") == "private"
            ):
                chat_id = chat.get("id")
                if chat_id not in (None, ""):
                    enqueue_task(task_queue.send_welcome, chat_id)
    except Exception:
        logger.exception("telegram_webhook failed to queue /start welcome")

    return Response({"ok": True}, status=200)


@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        logger.exception("healthz database check failed")
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


def index(request):
    return render(request, 'pages/index.html')


def upgrade(request):
    return render(request, 'pages/upgrade.html')


def _channel_chat_id(channel_username):
    value = str(channel_username or "").strip()
    if "t.me/" in value:
        value = value.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        value = value.split("/", 1)[0]
    if not value:
        return ""
    if value.startswith("@") or value.lstrip("-").isdigit():
        return value
    return f"@{value}"


def _channel_public_url(channel_username):
    value = str(channel_username or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    chat_id = _channel_chat_id(value)
    name = chat_id[1:] if chat_id.startswith("@") else chat_id
    return f"https://t.me/{name}" if name else "https://t.me/"


def telegram_user_in_channel(telegram_id, channel_username):
    chat_id = _channel_chat_id(channel_username)
    token = settings.TELEGRAM_BOT_TOKEN
    if not chat_id or not token or telegram_id in (None, ""):
        return False
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getChatMember",
            params={"chat_id": chat_id, "user_id": telegram_id},
            timeout=10,
        )
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        return status in ("member", "administrator", "creator")
    except Exception:
        return False


@api_view(['POST'])
@permission_classes([AllowAny])
def check_channel_membership(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    channel_username = request.data.get("channel_username")
    is_member = telegram_user_in_channel(telegram_id, channel_username)
    return Response({"is_member": is_member})





@api_view(['POST'])
@permission_classes([AllowAny])
def daily_check_in(request):
    init_data = request.data.get('initData')
    telegram_id = verify_telegram_data(init_data) if init_data else None
    if not telegram_id:
        return Response({"error": "Unauthorized"}, status=401)

    try:
        today = timezone.now().date()

        with transaction.atomic():
            user = User.objects.select_for_update().get(telegram_id=telegram_id)

            # لو اليوزر سجل النهاردة خلاص
            if user.last_daily_check == today:
                return Response({"error": "لقد سجلت دخولك اليوم بالفعل"}, status=400)

            # لو كان غايب يوم أو أكتر، نصفّر العداد
            if user.last_daily_check:
                days_diff = (today - user.last_daily_check).days
                if days_diff > 1:
                    user.daily_streak = 0

            # نزود اليوم
            user.daily_streak += 1
            user.last_daily_check = today

            reward_message = f"+{DAILY_CHECKIN_REWARD} TAC"

            # مكافأة اليوم 30 مرة واحدة، والعداد يفضل 30
            if user.daily_streak >= 30 and not user.has_claimed_30_day_gift:
                dragon_worker = Worker.objects.filter(name="Dragon").first()
                if dragon_worker:
                    uw, created = UserWorker.objects.get_or_create(user=user, worker=dragon_worker)
                    uw.level += 1
                    uw.save(update_fields=["level"])
                    reward_message = (
                        f"🎉 مبروك! حصلت على عامل Dragon + {DAILY_CHECKIN_REWARD} TAC"
                    )
                    user.has_claimed_30_day_gift = True

            user.balance += DAILY_CHECKIN_REWARD
            user.save(update_fields=[
                "balance",
                "daily_streak",
                "last_daily_check",
                "has_claimed_30_day_gift",
            ])

        return Response({
            "message": "Check-in Successful!",
            "new_balance": float(user.balance),
            "daily_streak": user.daily_streak,
            "reward_message": reward_message
        })
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)








@api_view(['GET'])
@permission_classes([AllowAny])
def get_leaderboard(request):
    top_users = User.objects.order_by('-balance')[:10]
    data = []
    for u in top_users:
        data.append({
            "username": u.username or "Unknown",
            "balance": float(u.balance)
        })
    return Response(data)


def tasks(request):
    channels = (
        settings.TELEGRAM_TASK_CHANNEL_1,
        settings.TELEGRAM_TASK_CHANNEL_2,
        settings.TELEGRAM_TASK_CHANNEL_3,
        settings.TELEGRAM_TASK_CHANNEL_4,
        getattr(settings, "TELEGRAM_TASK_CHANNEL_5", None),
        getattr(settings, "TELEGRAM_TASK_CHANNEL_6", None),
    )
    context = {}
    for index, channel in enumerate(channels, start=1):
        context[f"task_channel_{index}"] = channel or ""
        context[f"task_channel_{index}_url"] = _channel_public_url(channel)
    return render(request, 'pages/tasks.html', context)


def referral(request):
    return render(request, 'pages/referral.html')


def profile(request):
    return render(request, 'pages/profile.html')

def exchange(request):
    return render(request, 'pages/exchange.html')

def withdraw(request):
    return render(request, 'pages/withdraw.html')

def withdraw_history(request):
    return render(request, 'pages/withdraw-history.html')

def how_to_earn(request):
    return render(request, 'pages/how-to-earn.html')

def about(request):
    return render(request, 'pages/about.html')

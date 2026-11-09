from django.shortcuts import render
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from .models import User, Worker, UserWorker, UserTask, WithdrawRequest, PendingReferral 
from rest_framework.permissions import AllowAny
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
import requests
import random
from django.conf import settings
import threading
import time


# مكافآت دعوة الأصدقاء: اسم المهمة في UserTask -> (عدد الأصدقاء، المكافأة)
MILESTONE_REWARDS = {
    "milestone_3": (3, Decimal("30")),
    "milestone_5": (5, Decimal("50")),
    "milestone_10": (10, Decimal("100")),
}


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
def calculate_user_production(user):
    user_workers = UserWorker.objects.filter(user=user).select_related('worker')
    
    total_per_hour = 0
    for uw in user_workers:
        worker_production = float(uw.worker.base_price) * 0.01 * (1.1 ** uw.level)
        total_per_hour += worker_production
        
    # التحقق من تفعيل البوست
    now = timezone.now()
    multiplier = 1.0
    
    if user.last_boost_time:
        # لو عدت ساعة من وقت تفعيل البوست، بنلغي البوست
        if now > user.last_boost_time + timedelta(hours=1):
            multiplier = 1.0
            # نرجع السرعة لطبيعتها في الداتابيز
            if user.boost_multiplier != 1.0:
                user.boost_multiplier = 1.0
                user.save()
        else:
            multiplier = float(user.boost_multiplier)
            
    total_per_hour *= multiplier
    
    return {
        "per_hour": total_per_hour,
        "per_day": total_per_hour * 24,
        "per_month": total_per_hour * 24 * 30
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def login_or_register(request):
    telegram_id = request.data.get('telegram_id')
    username = request.data.get('username', '')
    referral_id = request.data.get('referral_id')

    if not telegram_id:
        return Response({"error": "Telegram ID is required"}, status=400)

    try:
        telegram_id = int(str(telegram_id).strip())
    except (TypeError, ValueError):
        return Response({"error": "Telegram ID is required"}, status=400)

    # بنشوف المستخدم موجود ولا جديد
    user, created = User.objects.get_or_create(
        telegram_id=telegram_id,
        defaults={'username': username}
    )

    offline_earnings = 0
    if user.last_collected_at:
        elapsed_seconds = (timezone.now() - user.last_collected_at).total_seconds()
        if elapsed_seconds > 60:  # لو عدت دقيقة على الأقل
            production = calculate_user_production(user)
            per_second = production["per_hour"] / 3600
            earned = Decimal(str(per_second * elapsed_seconds))
            if earned > 0:
                offline_earnings = float(earned)

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
    time_left = 0
    if user.last_boost_time:
        boost_end_time = user.last_boost_time + timedelta(hours=1)
        now = timezone.now()
        if now < boost_end_time:
            time_left = int((boost_end_time - now).total_seconds())
        else:
            if user.boost_multiplier != 1.0:
                user.boost_multiplier = 1.0
                user.save()

    # 8. بنحسب الإنتاج الحالي للعمال
    production = calculate_user_production(user)
    referred_users = list(user.referrals.all())
    referrals_list = []
    for ref in referred_users:
        referrals_list.append({
            "username": ref.username or str(ref.telegram_id),
            "balance": float(ref.balance)
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
            "ads_watched_today": user.ads_watched_today,
            "boost_ads_watched": user.boost_ads_watched,
            "last_boost_time": user.last_boost_time.isoformat() if user.last_boost_time else None,
            "time_left": time_left,
            "referrals": referrals_list,
            "referrals_count": referrals_count,
            "claimed_milestones": get_claimed_milestones(user),
            "production": production,
            "completed_tasks": [task.task_name for task in user.tasks.all()],
            "offline_earnings": offline_earnings,
        }
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def collect_gold(request):
    telegram_id = request.data.get('telegram_id')
    amount_to_collect = request.data.get('amount')

    # بنتأكد إن الداتا وصلت صح
    if not telegram_id:
        return Response({"error": "Telegram ID required"}, status=400)
    
    if amount_to_collect is None:
        return Response({"error": "Amount required"}, status=400)

    try:
        # بنحول المبلغ لرقم عشان نضمن إنه مش نص
        amount = Decimal(str(amount_to_collect))
        
        user = User.objects.get(telegram_id=telegram_id)
        if amount > 0:
            user.balance += amount
            user.save()

            # --- كود الإحالة الجديد (10% لصاحب الدعوة) ---
            if user.referred_by:
                referral_bonus = amount * Decimal('0.10') # 10%
                referrer = user.referred_by
                referrer.referral_balance += referral_bonus # بنضيف لرصيد الإحالة
                referrer.save()
            # ---------------------------------------------
        
        return Response({"message": "Collected!", "new_balance": float(user.balance)})
    
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception as e:
        # لو حصل أي خطأ تاني، هنطبعه في التيرمينال ونرجعه للفرونت
        print(f"ERROR in collect_gold: {e}")
        return Response({"error": str(e)}, status=500)
    



@api_view(['POST'])
@permission_classes([AllowAny])
def upgrade_worker(request):
    telegram_id = request.data.get('telegram_id')
    worker_id = request.data.get('worker_id')

    if not telegram_id or not worker_id:
        return Response({"error": "Missing data"}, status=400)

    try:
        user = User.objects.get(telegram_id=telegram_id)
        worker = Worker.objects.get(id=worker_id)
        
        user_worker, created = UserWorker.objects.get_or_create(user=user, worker=worker)
        
        current_level = user_worker.level
        price = worker.base_price * (Decimal('1.5') ** current_level)

        if user.balance >= price:
            user.balance -= price
            user_worker.level += 1
            user.save()
            user_worker.save()

            # احسب الإنتاج الجديد (بعد الترقية)
            production = calculate_user_production(user)

            return Response({
                "message": "Upgraded!", 
                "new_balance": float(user.balance),
                "new_level": user_worker.level,
                "production": production 
            })
        else:
            return Response({"error": "Insufficient balance"}, status=400)
            
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
    telegram_id = request.data.get('telegram_id')
    task_name = request.data.get('task_name')

    if not telegram_id or not task_name:
        return Response({"error": "Missing data"}, status=400)

    channel_tasks = {
        "channel_1": settings.TELEGRAM_TASK_CHANNEL_1,
        "channel_2": settings.TELEGRAM_TASK_CHANNEL_2,
        "channel_3": settings.TELEGRAM_TASK_CHANNEL_3,
        "channel_4": settings.TELEGRAM_TASK_CHANNEL_4,
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
                friends_count = user.referrals.count()
                if friends_count < required_friends:
                    return Response({"error": "عدد الأصدقاء المدعوين غير كافٍ"}, status=400)

                user.referral_balance += reward_amount
                user.save(update_fields=["referral_balance"])
                UserTask.objects.create(user=user, task_name=task_name)

                return Response({
                    "message": "Task Completed!",
                    "reward": float(reward_amount),
                    "referral_balance": float(user.referral_balance),
                    "new_balance": float(user.balance),
                    "claimed_milestones": get_claimed_milestones(user),
                    "referrals_count": friends_count,
                })

            # مكافآت المهام العادية بتضاف للرصيد الأساسي
            reward_amount = Decimal('50.00')
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
    telegram_id = request.data.get('telegram_id')
    wallet_address = request.data.get('wallet_address') # العنوان اللي جاي من TON Connect

    if not telegram_id or not wallet_address:
        return Response({"error": "Missing data"}, status=400)

    try:
        user = User.objects.get(telegram_id=telegram_id)
        if str(wallet_address).startswith("0:"):
            try:
                from tonsdk.utils import Address
                addr = Address(wallet_address)
                friendly_addr = addr.to_string(is_user_friendly=True, is_bounceable=False, is_url_safe=True)
                user.wallet_address = friendly_addr
            except:
                user.wallet_address = wallet_address
        else:
            user.wallet_address = wallet_address
        user.is_wallet_verified = True # بنوثق المحفظة
        user.save()

        return Response({"message": "Wallet Verified!", "is_wallet_verified": True})
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)


@api_view(['POST'])
@permission_classes([AllowAny])
def disconnect_wallet(request):
    telegram_id = request.data.get('telegram_id')
    if not telegram_id:
        return Response({"error": "Missing data"}, status=400)
    try:
        user = User.objects.get(telegram_id=telegram_id)
        user.wallet_address = None
        user.is_wallet_verified = False
        user.save()
        return Response({"message": "Wallet Disconnected!"})
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)




@api_view(['POST'])
@permission_classes([AllowAny])
def watch_ad(request):
    telegram_id = request.data.get('telegram_id')
    ad_type = request.data.get('ad_type')

    if not telegram_id or not ad_type:
        return Response({"error": "Missing data"}, status=400)

    try:
        user = User.objects.get(telegram_id=telegram_id)
        now = timezone.now()
        today = now.date()

        # --- 1. إعلانات التفعيل اليومي ---
        if ad_type == 'activation':
            if user.last_activation_date != today:
                user.ads_watched_today = 0
                user.last_activation_date = today

            if user.ads_watched_today < 3:
                user.ads_watched_today += 1
                reward = Decimal('1.00')
                user.balance += reward
                user.save()
                return Response({
                    "message": "Ad Watched!", 
                    "new_balance": float(user.balance),
                    "watched_count": user.ads_watched_today,
                    "reward": float(reward)
                })
            else:
                return Response({"error": "لقد أكملت إعلانات التفعيل اليوم"}, status=400)

        # --- 2. إعلانات المضاعفة (Boost) ---
                # --- 2. إعلانات المضاعفة (Boost) ---
        elif ad_type == 'boost':
            now = timezone.now()
            
            # 1. لو البوست شغال حالياً (يعني الساعة لسه مخلصتش)
            if user.last_boost_time and user.boost_multiplier > 1.0:
                boost_end_time = user.last_boost_time + timedelta(hours=1)
                if now < boost_end_time:
                    return Response({"error": "البوست شغال بالفعل. استنى لما يخلص."}, status=400)
                else:
                    # لو خلصت الساعة، بنلغي البوست ونرجع السرعة الطبيعية
                    user.boost_multiplier = 1.0
                    user.save()

            # بنشوف وقت بداية الدورة القديمة (لو موجود)
            if user.boost_ads_watched >= 5:
                # معناها إنه خلص الـ 5 إعلانات، ومحتاج يستنى 8 ساعات من وقت بداية الدورة
                if user.last_boost_time:
                    # بنحسب وقت انتهاء الاستراحة (8 ساعات من بداية الدورة)
                    cooldown_end = user.last_boost_time + timedelta(hours=8)
                    if now < cooldown_end:
                        time_left_cooldown = int((cooldown_end - now).total_seconds())
                        return Response({"error": f"لازم تستنى {time_left_cooldown} ثانية."}, status=400)
                    else:
                        # خلصت الـ 8 ساعات، بنصفر العداد عشان يبدأ دورة جديدة
                        user.boost_ads_watched = 0
                        user.last_boost_time = now
                        user.save()

            # 3. لو العداد أقل من 5، بنزوده
            if user.boost_ads_watched < 5:
                user.boost_ads_watched += 1
                user.save()
                return Response({
                    "message": "Boost Ad Watched!",
                    "watched_count": user.boost_ads_watched
                })
            else:
                # 4. لو وصل 5، بنديله المضاعف
                user.boost_multiplier = Decimal('2.00')
                user.last_boost_time = now
                user.save()
                return Response({
                    "message": "Boost Activated!",
                    "boost_multiplier": float(user.boost_multiplier)
                })

        return Response({"error": "Invalid ad type"}, status=400)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



@api_view(['POST'])
@permission_classes([AllowAny])
def exchange_currency(request):
    telegram_id = request.data.get('telegram_id')
    tac_amount = request.data.get('tac_amount')

    if not telegram_id or not tac_amount:
        return Response({"error": "Missing data"}, status=400)

    try:
        tac_amount = Decimal(str(tac_amount))
        if tac_amount < 920:
            return Response({"error": "الحد الأدنى 920 TAC"}, status=400)

        user = User.objects.get(telegram_id=telegram_id)
        
        if user.balance < tac_amount:
            return Response({"error": "رصيدك غير كافي"}, status=400)

        # حساب الـ USDT (كل 920 TAC = 1 USDT)
        usdt_added = tac_amount / Decimal('920.0')

        # خصم الـ TAC وإضافة الـ USDT
        user.balance -= tac_amount
        user.usdt_balance += usdt_added
        user.save()

        return Response({
            "message": "Exchange Successful!",
            "new_tac_balance": float(user.balance),
            "new_usdt_balance": float(user.usdt_balance),
            "usdt_added": float(usdt_added)
        })

    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(['POST'])
@permission_classes([AllowAny])
def collect_referral_gold(request):
    telegram_id = request.data.get('telegram_id')
    
    if not telegram_id:
        return Response({"error": "Missing data"}, status=400)
        
    try:
        user = User.objects.get(telegram_id=telegram_id)
        
        # بنشوف لو فيه رصيد إحالة متاح
        if user.referral_balance <= 0:
            return Response({"error": "لا يوجد رصيد إحالة للسحب"}, status=400)
            
        amount_to_collect = user.referral_balance
        
        # بنضيف رصيد الإحالة للرصيد الأساسي
        user.balance += amount_to_collect
        user.referral_balance = 0 # بنصفر رصيد الإحالة
        user.save()
        
        return Response({
            "message": "Referral Gold Collected!",
            "new_balance": float(user.balance),
            "collected_amount": float(amount_to_collect)
        })
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



@api_view(['POST'])
@permission_classes([AllowAny])
def request_withdraw(request):
    telegram_id = request.data.get('telegram_id')
    amount = request.data.get('amount')
    wallet_address = request.data.get('wallet_address')

    if not telegram_id or not amount or not wallet_address:
        return Response({"error": "Missing data"}, status=400)

    try:
        user = User.objects.get(telegram_id=telegram_id)
        withdraw_amount = Decimal(str(amount))

        if withdraw_amount < 5:
            return Response({"error": "الحد الأدنى للسحب 5 USDT"}, status=400)

        if user.usdt_balance < withdraw_amount:
            return Response({"error": "رصيدك غير كافي"}, status=400)

        # 1. خصم الرصيد فوراً
        user.usdt_balance -= withdraw_amount
        user.save()

        # 2. تسجيل الطلب في الداتابيز
        WithdrawRequest.objects.create(
            user=user,
            amount=withdraw_amount,
            wallet_address=wallet_address,
            status='pending'
        )

        # 3. إرسال إشعار لقناة الأدمن (في الخلفية)
        thread_admin = threading.Thread(
            target=send_admin_notification,
            args=(user.username, float(withdraw_amount), wallet_address)
        )
        thread_admin.start()

        # 4. إرسال الإعلان لقناة الجمهور (في الخلفية)
        thread_public = threading.Thread(
            target=send_public_announcement,
            args=(user.username, float(withdraw_amount), wallet_address)
        )
        thread_public.start()

        return Response({
            "message": "Withdraw Request Submitted!",
            "new_usdt_balance": float(user.usdt_balance)
        })

    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)
    except Exception as e:
        return Response({"error": str(e)}, status=500)
    



@api_view(['GET'])
@permission_classes([AllowAny])
def get_withdraw_history(request):
    telegram_id = request.query_params.get('telegram_id')
    
    if not telegram_id:
        return Response({"error": "Missing telegram_id"}, status=400)
        
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
                "date": w.created_at.strftime("%Y-%m-%d %H:%M")
            })
            
        return Response(data)
    except User.DoesNotExist:
        return Response({"error": "User not found"}, status=404)



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

👤 **المستخدم:** {username}
💰 **المبلغ:** {amount:.2f} USDT
💳 **المحفظة:** `{wallet_address}`

⏳ **الحالة:** قيد المراجعة
    """
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_ADMIN_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending admin notification: {e}")





# 2. دالة إرسال إعلان لقناة الجمهور (بعد وقت عشوائي)
def send_public_announcement(username, amount, wallet_address):
    # تأخير عشوائي (3 لـ 10 دقايق)
    delay = random.randint(180, 600)
    time.sleep(delay)

    message = f"""
🎉 **تم صرف أرباح جديدة!** 🎉

👤 **المستخدم:** {username}
💰 **المبلغ:** {amount:.2f} USDT
💳 **المحفظة:** `{wallet_address[:10]}...{wallet_address[-5:]}`

🚀 **كن أنت التالي!** العب واجمع وسحب أرباحك الآن.
    """
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": settings.TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending public announcement: {e}")








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
    try:
        message = request.data.get("message") or request.data.get("edited_message") or {}
        if isinstance(message, dict):
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
        pass
    return Response({"ok": True}, status=200)


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
    telegram_id = request.data.get("telegram_id")
    channel_username = request.data.get("channel_username")
    is_member = telegram_user_in_channel(telegram_id, channel_username)
    return Response({"is_member": is_member})


def tasks(request):
    channels = (
        settings.TELEGRAM_TASK_CHANNEL_1,
        settings.TELEGRAM_TASK_CHANNEL_2,
        settings.TELEGRAM_TASK_CHANNEL_3,
        settings.TELEGRAM_TASK_CHANNEL_4,
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

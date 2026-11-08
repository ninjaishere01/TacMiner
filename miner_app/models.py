from django.db import models

# 1. جدول المستخدمين (اللاعبين)
class User(models.Model):
    telegram_id = models.BigIntegerField(unique=True, verbose_name="Telegram ID")
    username = models.CharField(max_length=150, blank=True, null=True, verbose_name="Username")
    first_name = models.CharField(max_length=150, blank=True, null=True, verbose_name="First Name")

    # الحقل الجديد
    referral_balance = models.DecimalField(max_digits=18, decimal_places=8, default=0.00000000, verbose_name="رصيد الإحالات") # <-- ضيف ده
    # الحقل الجديد: مين اللي دعا اليوزر ده
    referred_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals', verbose_name="دعوة من")
    # رقم صاحب الدعوة لو وصل من /start ولسه ما اتربطش
    pending_referral = models.BigIntegerField(null=True, blank=True, verbose_name="إحالة معلّقة")
    # الأرصدة
    balance = models.DecimalField(max_digits=18, decimal_places=8, default=0.00000000, verbose_name="رصيد TAC")
    usdt_balance = models.DecimalField(max_digits=18, decimal_places=8, default=0.00000000, verbose_name="رصيد USDT")

    # حقول الإعلانات
    ads_watched_today = models.IntegerField(default=0, verbose_name="إعلانات التفعيل اليوم")
    last_activation_date = models.DateField(null=True, blank=True, verbose_name="تاريخ آخر تفعيل")
    
    boost_ads_watched = models.IntegerField(default=0, verbose_name="إعلانات المضاعفة")
    last_boost_time = models.DateTimeField(null=True, blank=True, verbose_name="وقت آخر مضاعفة")
    boost_multiplier = models.DecimalField(max_digits=5, decimal_places=2, default=1.00, verbose_name="مضاعف السرعة")
    
    # المحفظة
    wallet_address = models.CharField(max_length=255, blank=True, null=True, verbose_name="عنوان المحفظة")
    is_wallet_verified = models.BooleanField(default=False, verbose_name="هل المحفظة موثقة؟")
    is_partner = models.BooleanField(default=False, verbose_name="هل هو شريك؟ (صاحب جروب)") 

    
    # التواريخ
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الانضمام")
    last_activity = models.DateTimeField(auto_now=True, verbose_name="آخر نشاط")

    def __str__(self):
        return f"{self.username or self.telegram_id}"

# 2. جدول العمال (البيانات الثابتة)
class Worker(models.Model):
    name = models.CharField(max_length=50, verbose_name="اسم العامل")
    icon = models.CharField(max_length=50, help_text="FontAwesome icon class", verbose_name="الأيقونة")
    image = models.CharField(max_length=255, help_text="URL or static path", verbose_name="مسار الصورة")
    base_price = models.DecimalField(max_digits=18, decimal_places=2, verbose_name="السعر الأساسي")

    def __str__(self):
        return self.name

# 3. جدول عمال المستخدم (المستويات)
class UserWorker(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="workers", verbose_name="المستخدم")
    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, verbose_name="العامل")
    level = models.IntegerField(default=0, verbose_name="المستوى")

    class Meta:
        unique_together = ('user', 'worker') # عشان المستخدم ميكونش معاه نفس العامل مرتين في نفس السطر

    def __str__(self):
        return f"{self.user.username} - {self.worker.name} (Lvl {self.level})"



class UserTask(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tasks", verbose_name="المستخدم")
    task_name = models.CharField(max_length=100, verbose_name="اسم المهمة")
    completed_at = models.DateTimeField(auto_now_add=True, verbose_name="وقت الإكمال")

    class Meta:
        unique_together = ('user', 'task_name') # عشان ميعملش نفس المهمة مرتين

    def __str__(self):
        return f"{self.user.username} - {self.task_name}"



class WithdrawRequest(models.Model):
    STATUS_CHOICES = (
        ('pending', 'قيد المراجعة'),
        ('completed', 'تم الدفع'),
        ('rejected', 'مرفوض'),
    )
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="withdrawals", verbose_name="المستخدم")
    amount = models.DecimalField(max_digits=18, decimal_places=8, verbose_name="المبلغ (USDT)")
    wallet_address = models.CharField(max_length=255, verbose_name="عنوان المحفظة")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="الحالة")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الطلب")

    def __str__(self):
        return f"{self.user.username} - {self.amount} USDT - {self.status}"


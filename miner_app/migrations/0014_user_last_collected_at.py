import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('miner_app', '0013_alter_user_last_seen'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='last_collected_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now, verbose_name='آخر مرة تم تحديث الرصيد'),
            preserve_default=False,
        ),
        migrations.RemoveField(
            model_name='user',
            name='last_seen',
        ),
    ]

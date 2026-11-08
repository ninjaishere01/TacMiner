import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('miner_app', '0011_pendingreferral'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='last_seen',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now, verbose_name='آخر مرة دخل'),
            preserve_default=False,
        ),
    ]

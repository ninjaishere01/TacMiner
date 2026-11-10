from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('miner_app', '0017_user_daily_streak_user_last_daily_check'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='has_claimed_30_day_gift',
            field=models.BooleanField(default=False, verbose_name='اخد مكافأة اليوم 30'),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('miner_app', '0009_user_referred_by_alter_user_id_alter_usertask_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='pending_referral',
            field=models.BigIntegerField(blank=True, null=True, verbose_name='إحالة معلّقة'),
        ),
    ]

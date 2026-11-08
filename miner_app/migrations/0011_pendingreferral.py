from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('miner_app', '0010_user_pending_referral'),
    ]

    operations = [
        migrations.CreateModel(
            name='PendingReferral',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('telegram_id', models.BigIntegerField(unique=True)),
                ('referral_id', models.BigIntegerField()),
            ],
        ),
    ]

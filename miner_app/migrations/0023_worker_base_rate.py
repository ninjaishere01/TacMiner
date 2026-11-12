from django.db import migrations, models


# الأسعار بعد التنصيف، والإنتاج/ساعة عند المستوى 0.
# القيم دي بتطابق seed_workers؛ الميجريشن بيحدّث العمال الموجودين بالفعل.
WORKER_CONFIG = {
    "Alpha": ("50", "2.5"),
    "Dragon": ("125", "6.25"),
    "Hawk": ("250", "12.5"),
    "Killer": ("500", "25"),
    "Puglist": ("1250", "62.5"),
    "Shooter": ("2500", "125"),
    "Romeo": ("5000", "250"),
    "Detector": ("12500", "625"),
}


def apply_worker_prices(apps, schema_editor):
    Worker = apps.get_model("miner_app", "Worker")
    for name, (price, rate) in WORKER_CONFIG.items():
        Worker.objects.filter(name=name).update(base_price=price, base_rate=rate)


class Migration(migrations.Migration):

    dependencies = [
        ("miner_app", "0022_adsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="worker",
            name="base_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=4,
                max_digits=18,
                null=True,
                verbose_name="الإنتاج/ساعة (المستوى 0)",
            ),
        ),
        migrations.RunPython(apply_worker_prices, migrations.RunPython.noop),
    ]

from django.core.management.base import BaseCommand

from miner_app.models import Worker


WORKERS_DATA = [
    {"name": "Alpha", "icon": "fa-person-digging", "image": "/static/images/1.png", "base_price": 100},
    {"name": "Dragon", "icon": "fa-dragon", "image": "/static/images/2.png", "base_price": 250},
    {"name": "Hawk", "icon": "fa-crow", "image": "/static/images/3.png", "base_price": 500},
    {"name": "Killer", "icon": "fa-user-ninja", "image": "/static/images/4.png", "base_price": 1000},
    {"name": "Puglist", "icon": "fa-hand-fist", "image": "/static/images/5.png", "base_price": 2500},
    {"name": "Shooter", "icon": "fa-gun", "image": "/static/images/6.png", "base_price": 5000},
    {"name": "Romeo", "icon": "fa-user-tie", "image": "/static/images/7.png", "base_price": 10000},
    {"name": "Detector", "icon": "fa-magnifying-glass", "image": "/static/images/8.png", "base_price": 25000},
]


class Command(BaseCommand):
    help = "ينشئ العمال الناقصين فقط من غير ما يغيّر أسعار العمال الموجودين."

    def handle(self, *args, **options):
        created_count = 0
        for worker_data in WORKERS_DATA:
            worker, created = Worker.objects.get_or_create(
                name=worker_data["name"],
                defaults=worker_data,
            )
            if created:
                created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Workers created: {created_count} | total workers: {Worker.objects.count()}"
            )
        )

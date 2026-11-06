import os
import django

# إعداد بيئة Django عشان السكريبت يشتغل بره السيرفر
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend_core.settings')
django.setup()

from miner_app.models import Worker

workers_data = [
    {"name": "Alpha", "icon": "fa-person-digging", "image": "/static/images/1.png", "base_price": 100},
    {"name": "Dragon", "icon": "fa-dragon", "image": "/static/images/2.png", "base_price": 250},
    {"name": "Hawk", "icon": "fa-crow", "image": "/static/images/3.png", "base_price": 500},
    {"name": "Killer", "icon": "fa-user-ninja", "image": "/static/images/4.png", "base_price": 1000},
    {"name": "Puglist", "icon": "fa-hand-fist", "image": "/static/images/5.png", "base_price": 2500},
    {"name": "Shooter", "icon": "fa-gun", "image": "/static/images/6.png", "base_price": 5000},
    {"name": "Romeo", "icon": "fa-user-tie", "image": "/static/images/7.png", "base_price": 10000},
    {"name": "Detector", "icon": "fa-magnifying-glass", "image": "/static/images/8.png", "base_price": 25000},
]

for w in workers_data:
    Worker.objects.get_or_create(name=w["name"], defaults=w)

print("✅ تم إضافة العمال الثمانية بنجاح إلى قاعدة البيانات!")
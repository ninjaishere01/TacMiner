from django.urls import path
from .import views




urlpatterns = [
    path('', views.index , name= 'index'),
    path('upgrade/', views.upgrade , name= 'upgrade'),
    path('tasks/', views.tasks, name='tasks'),
    path('referral/', views.referral, name='referral'),
    path('profile/', views.profile, name='profile'),
    path('exchange/', views.exchange, name='exchange'),
    path('withdraw/', views.withdraw, name='withdraw'),
    path('withdraw-history/', views.withdraw_history, name='withdraw_history'),
    path('how-to-earn/', views.how_to_earn, name='how_to_earn'),
    path('about/', views.about, name='about'),


    path('api/login/', views.login_or_register, name='api_login'),
    path('api/telegram-webhook/', views.telegram_webhook, name='telegram_webhook'),
    path('api/telegram/webhook/', views.telegram_webhook, name='telegram_webhook_legacy'),
    path('api/collect/', views.collect_gold, name='api_collect'),   
    path('api/upgrade/', views.upgrade_worker, name='api_upgrade'), 
    path('api/workers/', views.get_workers, name='api_workers'),
    path('api/verify-wallet/', views.verify_wallet, name='api_verify_wallet'),
    path('api/exchange/', views.exchange_currency, name='api_exchange'),

    # AdsGram
    path('api/claim-task/', views.claim_task_reward, name='api_claim_task'),
    path('api/watch-ad/', views.watch_ad, name='api_watch_ad'),

    path('api/collect-referral/', views.collect_referral_gold, name='api_collect_referral'),

    path('api/request-withdraw/', views.request_withdraw, name='api_request_withdraw'),
    path('api/withdraw-history/', views.get_withdraw_history, name='api_withdraw_history'),

]


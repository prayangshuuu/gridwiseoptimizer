from django.urls import path
from .views import HealthCheckView, OptimizeEnergyView

urlpatterns = [
    path('health', HealthCheckView.as_view(), name='health'),
    path('optimize-energy', OptimizeEnergyView.as_view(), name='optimize-energy'),
]

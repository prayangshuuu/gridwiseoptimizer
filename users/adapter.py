from allauth.account.adapter import DefaultAccountAdapter
from django.urls import reverse

class CustomAccountAdapter(DefaultAccountAdapter):
    def get_login_redirect_url(self, request):
        if request.user.is_authenticated and request.user.role == 'admin':
            return reverse('admin:index')
        return super().get_login_redirect_url(request)

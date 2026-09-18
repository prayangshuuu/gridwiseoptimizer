from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        USER = 'user', 'User'

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.USER)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.role == self.Role.ADMIN:
            self.is_staff = True
            self.is_superuser = True
        super().save(*args, **kwargs)

    @property
    def is_role_admin(self):
        return self.role == self.Role.ADMIN

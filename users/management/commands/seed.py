import os
import sys
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

User = get_user_model()

class Command(BaseCommand):
    help = 'Seed database with initial data (admin and regular user)'

    def handle(self, *args, **options):
        admin_email = os.environ.get('SEED_ADMIN_EMAIL')
        admin_password = os.environ.get('SEED_ADMIN_PASSWORD')
        user_email = os.environ.get('SEED_USER_EMAIL')
        user_password = os.environ.get('SEED_USER_PASSWORD')

        if not admin_email or not admin_password:
            raise CommandError("SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD environment variables are required.")

        if not user_email or not user_password:
            raise CommandError("SEED_USER_EMAIL and SEED_USER_PASSWORD environment variables are required.")

        # Create admin
        if not User.objects.filter(email=admin_email).exists():
            User.objects.create_superuser(
                username=admin_email.split('@')[0] + '_admin',
                email=admin_email,
                password=admin_password,
                role=User.Role.ADMIN
            )
            self.stdout.write(self.style.SUCCESS(f'Admin user created: {admin_email}'))
        else:
            self.stdout.write(self.style.WARNING(f'Admin user already exists: {admin_email}'))

        # Create user
        if not User.objects.filter(email=user_email).exists():
            User.objects.create_user(
                username=user_email.split('@')[0] + '_user',
                email=user_email,
                password=user_password,
                role=User.Role.USER
            )
            self.stdout.write(self.style.SUCCESS(f'Regular user created: {user_email}'))
        else:
            self.stdout.write(self.style.WARNING(f'Regular user already exists: {user_email}'))

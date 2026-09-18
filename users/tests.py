from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.management import call_command
import os

User = get_user_model()

class UserTest(TestCase):
    def test_create_user(self):
        user = User.objects.create_user(
            username='testuser', 
            email='test@example.com', 
            password='password123'
        )
        self.assertEqual(user.email, 'test@example.com')
        self.assertEqual(user.role, User.Role.USER)
        self.assertFalse(user.is_superuser)

    def test_create_superuser(self):
        admin = User.objects.create_superuser(
            username='testadmin', 
            email='admin@example.com', 
            password='password123',
            role=User.Role.ADMIN
        )
        self.assertEqual(admin.email, 'admin@example.com')
        self.assertEqual(admin.role, User.Role.ADMIN)
        self.assertTrue(admin.is_superuser)

class SeedTest(TestCase):
    def setUp(self):
        os.environ['SEED_ADMIN_EMAIL'] = 'seed_admin@example.com'
        os.environ['SEED_ADMIN_PASSWORD'] = 'seed_admin_pass'
        os.environ['SEED_USER_EMAIL'] = 'seed_user@example.com'
        os.environ['SEED_USER_PASSWORD'] = 'seed_user_pass'

    def tearDown(self):
        del os.environ['SEED_ADMIN_EMAIL']
        del os.environ['SEED_ADMIN_PASSWORD']
        del os.environ['SEED_USER_EMAIL']
        del os.environ['SEED_USER_PASSWORD']

    def test_seed_command(self):
        call_command('seed')
        self.assertTrue(User.objects.filter(email='seed_admin@example.com').exists())
        self.assertTrue(User.objects.filter(email='seed_user@example.com').exists())

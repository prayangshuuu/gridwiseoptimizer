from django.test import TestCase, Client
from django.urls import reverse

class HealthCheckTest(TestCase):
    def test_health_endpoint(self):
        client = Client()
        response = client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

from django.db import models


class OptimizationRun(models.Model):
    """Best-effort audit log of an /optimize-energy call.

    Writing a row is optional and must never block or fail the API response
    (see gridwise.views), so this model is intentionally simple and lenient.
    """

    scenario_id = models.CharField(max_length=255)
    total_grid_kwh = models.FloatField()
    total_cost_bdt = models.FloatField()
    peak_grid_kwh = models.FloatField()
    request_payload = models.JSONField()
    response_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"OptimizationRun({self.scenario_id} @ {self.created_at:%Y-%m-%d %H:%M})"

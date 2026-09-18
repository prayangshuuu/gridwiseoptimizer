from django.contrib import admin

from .models import OptimizationRun


@admin.register(OptimizationRun)
class OptimizationRunAdmin(admin.ModelAdmin):
    list_display = (
        "scenario_id",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("scenario_id",)
    readonly_fields = tuple(f.name for f in OptimizationRun._meta.fields)

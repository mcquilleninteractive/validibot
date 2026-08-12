from django.contrib import admin
from django.shortcuts import redirect

from validibot.core.models import SiteSettings
from validibot.core.models import SupportMessage


# Register your models here.
@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "subject",
        "created",
    )
    list_filter = ("created", "user")
    search_fields = ("subject", "message", "user__username", "user__email")
    readonly_fields = ("created", "modified")
    ordering = ("-created",)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Singleton admin — clicking 'Site settings' goes straight to the edit page."""

    readonly_fields = (
        "slug",
        "created",
        "modified",
    )
    fieldsets = (
        (
            "Submission Policy",
            {
                "fields": (
                    "metadata_key_value_only",
                    "metadata_max_bytes",
                    "metadata_max_depth",
                ),
                "description": (
                    "Controls how submission metadata is validated when "
                    "workflows are started via the API."
                ),
            },
        ),
        (
            "Metadata",
            {
                "fields": (
                    "slug",
                    "created",
                    "modified",
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Skip the list page — go straight to the singleton edit page."""
        obj, _ = SiteSettings.objects.get_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
        )
        return redirect(
            f"{request.path}{obj.pk}/change/",
        )

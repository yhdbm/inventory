from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User

from .models import (
    Asset,
    Correction,
    EditStrategy,
    ExpectedItem,
    FieldStrategyMark,
    ReconciliationSession,
    SavedFilter,
    ScannedItem,
    UserProfile,
    Warehouse,
)


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "station_prefixes", "location_regex",
                    "has_snow_ids", "is_default", "position")
    list_editable = ("position",)
    ordering = ("position", "code")

    @admin.display(boolean=True, description="SNOW location ids")
    def has_snow_ids(self, obj):
        """Whether SNOW deep links can scope by location for this site.

        False means the links fall back to a Station ID STARTSWITH query, which
        is exact for tagged assets but cannot see untagged ones — surfaced as a
        column so the gap is visible rather than discovered later.
        """
        return bool(obj.snow_sys_ids)


class UserProfileInline(admin.StackedInline):
    """Warehouse assignment, edited on the user's own admin page.

    ``can_delete=False`` + ``min_num=1``: a user without a profile silently
    falls back to the default site, which is confusing to debug, so the form
    always shows the field.
    """

    model = UserProfile
    can_delete = False
    min_num = 1
    max_num = 1
    verbose_name_plural = "Warehouse assignment"


class UserAdmin(DjangoUserAdmin):
    inlines = [UserProfileInline]
    list_display = DjangoUserAdmin.list_display + ("warehouse_code",)

    @admin.display(description="warehouse", ordering="profile__warehouse__code")
    def warehouse_code(self, obj):
        profile = getattr(obj, "profile", None)
        if profile and profile.warehouse_id:
            return profile.warehouse.code
        return "(default)"


# Replace the stock User admin so the warehouse field sits with the account.
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


class ExpectedItemInline(admin.TabularInline):
    model = ExpectedItem
    extra = 0


class ScannedItemInline(admin.TabularInline):
    model = ScannedItem
    extra = 0


@admin.register(ReconciliationSession)
class ReconciliationSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "asset_tag_column", "created_at")
    search_fields = ("name",)
    inlines = [ExpectedItemInline, ScannedItemInline]


@admin.register(ExpectedItem)
class ExpectedItemAdmin(admin.ModelAdmin):
    list_display = ("asset_tag", "session", "row_number")
    list_filter = ("session",)
    search_fields = ("asset_tag",)


@admin.register(ScannedItem)
class ScannedItemAdmin(admin.ModelAdmin):
    list_display = ("asset_tag", "session", "scanned_by", "scanned_at")
    list_filter = ("session",)
    search_fields = ("asset_tag",)


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        "asset_tag", "serial_number", "model_category", "model",
        "state", "location", "assigned_to", "source_tab",
    )
    list_filter = ("state", "model_category", "manufacturer", "source_tab")
    search_fields = ("asset_tag", "serial_number", "display_name", "name", "assigned_to")
    list_per_page = 50


@admin.register(SavedFilter)
class SavedFilterAdmin(admin.ModelAdmin):
    list_display = ("field", "value", "use_count", "last_used")
    list_filter = ("field",)
    search_fields = ("value",)


@admin.register(EditStrategy)
class EditStrategyAdmin(admin.ModelAdmin):
    list_display = ("position", "name")
    list_editable = ("name",)
    ordering = ("position",)


@admin.register(FieldStrategyMark)
class FieldStrategyMarkAdmin(admin.ModelAdmin):
    list_display = ("field", "strategy")
    list_filter = ("strategy",)
    search_fields = ("field",)


@admin.register(Correction)
class CorrectionAdmin(admin.ModelAdmin):
    list_display = ("asset_tag", "change_count", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("asset_tag",)
    readonly_fields = ("current", "expected", "changes", "created_at", "updated_at")

    @admin.display(description="changes")
    def change_count(self, obj):
        return len(obj.changes)

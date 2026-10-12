from django import forms
from django.contrib import admin
from .models import (
    Organisation,
    StaffProfile,
    BloodInventory,
    RegisteredDonor,
    EmergencyRequest,
    AuditLog,
)


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ('name', 'org_type', 'district', 'phone', 'is_registered', 'is_active', 'created_at')
    list_filter = ('org_type', 'district', 'is_registered', 'is_active')
    search_fields = ('name', 'district', 'phone', 'address')


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'organisation', 'get_org_type')
    list_filter = ('organisation__org_type', 'organisation__district')
    search_fields = ('user__username', 'user__email', 'organisation__name')

    def get_org_type(self, obj):
        return obj.organisation.get_org_type_display()
    get_org_type.short_description = 'Role / Org Type'


@admin.register(BloodInventory)
class BloodInventoryAdmin(admin.ModelAdmin):
    list_display = ('bag_number', 'blood_type', 'volume_ml', 'expiry_date', 'status', 'owner', 'source_donor')
    list_filter = ('blood_type', 'status', 'owner__district', 'owner__org_type')
    search_fields = ('bag_number', 'owner__name')
    ordering = ('expiry_date',)


class RegisteredDonorAdminForm(forms.ModelForm):
    government_id_input = forms.CharField(
        label="Government / Civic ID",
        required=False,
        help_text="Enter ID for automatic HMAC-SHA256 hashing. The raw ID is never stored in the database.",
        widget=forms.TextInput(attrs={'class': 'vTextField'})
    )

    class Meta:
        model = RegisteredDonor
        fields = [
            'full_name',
            'blood_type',
            'contact_number',
            'district',
            'latitude',
            'longitude',
            'last_donated_date',
            'is_available_for_emergency',
        ]

    def save(self, commit=True):
        instance = super().save(commit=False)
        gov_input = self.cleaned_data.get('government_id_input')
        if gov_input:
            instance.government_id_hash = RegisteredDonor.hash_government_id(gov_input)
        if commit:
            instance.save()
        return instance


@admin.register(RegisteredDonor)
class RegisteredDonorAdmin(admin.ModelAdmin):
    form = RegisteredDonorAdminForm
    list_display = (
        'full_name', 'blood_type', 'district', 'contact_number',
        'last_donated_date', 'cooldown_status', 'is_available_for_emergency'
    )
    list_filter = ('blood_type', 'district', 'is_available_for_emergency')
    search_fields = ('full_name', 'district', 'contact_number')
    readonly_fields = ('government_id_hash',)

    def cooldown_status(self, obj):
        days = obj.cooldown_days_remaining
        if days == 0:
            return "Eligible (0 days)"
        return f"Cooldown Active ({days} days remaining)"
    cooldown_status.short_description = "Cooldown Status"


@admin.register(EmergencyRequest)
class EmergencyRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'requesting_hospital', 'required_blood_type', 'patient_reference',
        'status', 'blood_bank', 'fulfilling_organisation', 'allocated_bag', 'created_at'
    )
    list_filter = ('status', 'required_blood_type', 'requesting_hospital__district')
    search_fields = ('requesting_hospital__name', 'patient_reference')
    ordering = ('-created_at',)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'action', 'actor', 'organisation', 'bag_number', 'emergency_request')
    list_filter = ('action', 'organisation', 'created_at')
    search_fields = ('bag_number', 'actor__username', 'organisation__name')
    ordering = ('-created_at',)

    # Make AuditLog read-only in admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

from django import forms
from django.utils import timezone

from .models import (
    BLOOD_TYPE_CHOICES,
    BloodInventory,
    EmergencyRequest,
    Organisation,
    RegisteredDonor,
)
from .services import haversine_distance


class EmergencyRequestForm(forms.ModelForm):
    blood_bank = forms.ModelChoiceField(
        queryset=Organisation.objects.filter(org_type='BLOOD_BANK', is_registered=True, is_active=True).order_by('name'),
        required=False,
        empty_label="— Check Own Hospital Stock First (Default) —",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_blood_bank'}),
        help_text="Optional: Select a regional blood bank directly, or leave default to check hospital inventory first."
    )

    class Meta:
        model = EmergencyRequest
        fields = ['required_blood_type', 'patient_reference', 'blood_bank']
        widgets = {
            'required_blood_type': forms.Select(attrs={'class': 'form-select', 'required': True, 'id': 'id_required_blood_type'}),
            'patient_reference': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g. ICU-BED-12 or CASE-892',
                'required': True,
                'id': 'id_patient_reference',
            }),
        }
        help_texts = {
            'patient_reference': '⚠️ Do not enter real patient names or IDs. Use anonymized bed or case numbers only.',
        }

    def __init__(self, *args, hospital=None, **kwargs):
        super().__init__(*args, **kwargs)
        if hospital:
            banks = Organisation.objects.filter(org_type='BLOOD_BANK', is_registered=True, is_active=True)
            bank_list = []
            for b in banks:
                dist = haversine_distance(hospital.latitude, hospital.longitude, b.latitude, b.longitude)
                bank_list.append((b, dist))
            bank_list.sort(key=lambda x: x[1])

            choices = [('', "— Check Own Hospital Stock First (Default) —")]
            for b, dist in bank_list:
                choices.append((b.id, f"{b.name} ({b.district} • {dist:.1f} km away)"))
            self.fields['blood_bank'].choices = choices


class BloodInventoryForm(forms.ModelForm):
    class Meta:
        model = BloodInventory
        fields = ['bag_number', 'blood_type', 'volume_ml', 'expiry_date']
        widgets = {
            'bag_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BAG-A-POS-1001', 'required': True}),
            'blood_type': forms.Select(attrs={'class': 'form-select', 'required': True}),
            'volume_ml': forms.NumberInput(attrs={'class': 'form-control', 'value': 450, 'min': 100, 'max': 600, 'required': True}),
            'expiry_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'required': True}),
        }
        help_texts = {
            'bag_number': 'Unique inventory unit identifier.',
            'expiry_date': 'Standard CPDA-1 storage expiration date.',
        }

    def clean_expiry_date(self):
        expiry = self.cleaned_data.get('expiry_date')
        if expiry and expiry < timezone.now().date():
            raise forms.ValidationError("Expiry date cannot be in the past.")
        return expiry


class RecordDonationForm(forms.Form):
    donor = forms.ModelChoiceField(
        queryset=RegisteredDonor.objects.all(),
        widget=forms.Select(attrs={'class': 'form-select', 'required': True}),
        help_text="Select voluntary donor. Donor must not be in active cooldown."
    )
    bag_number = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. BAG-DON-2041', 'required': True})
    )
    volume_ml = forms.IntegerField(
        initial=450,
        min_value=100,
        max_value=600,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'required': True})
    )
    expiry_date = forms.DateField(
        widget=forms.DateInput(attrs={'class': 'form-control', 'type': 'date', 'required': True})
    )

    def clean_expiry_date(self):
        expiry = self.cleaned_data.get('expiry_date')
        if expiry and expiry < timezone.now().date():
            raise forms.ValidationError("Expiry date cannot be in the past.")
        return expiry

    def clean_donor(self):
        donor = self.cleaned_data.get('donor')
        if donor and donor.cooldown_days_remaining > 0:
            raise forms.ValidationError(
                f"Donor is currently in medical cooldown ({donor.cooldown_days_remaining} days remaining). Whole blood donation rejected."
            )
        return donor


class RejectRequestForm(forms.Form):
    rejection_note = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': 'State clinical reason for rejection...',
            'required': True,
        }),
        help_text="A rejection reason is required before returning the request to PENDING."
    )


class CSVImportForm(forms.Form):
    csv_file = forms.FileField(
        label="Select CSV File",
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.csv', 'required': True}),
        help_text="Upload a valid .csv file encoded in UTF-8."
    )

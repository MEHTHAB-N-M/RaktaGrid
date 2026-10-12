import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .managers import ActiveDonorManager

KERALA_DISTRICTS = [
    ('Thiruvananthapuram', 'Thiruvananthapuram'),
    ('Kollam', 'Kollam'),
    ('Pathanamthitta', 'Pathanamthitta'),
    ('Alappuzha', 'Alappuzha'),
    ('Kottayam', 'Kottayam'),
    ('Idukki', 'Idukki'),
    ('Ernakulam', 'Ernakulam'),
    ('Thrissur', 'Thrissur'),
    ('Palakkad', 'Palakkad'),
    ('Malappuram', 'Malappuram'),
    ('Kozhikode', 'Kozhikode'),
    ('Wayanad', 'Wayanad'),
    ('Kannur', 'Kannur'),
    ('Kasaragod', 'Kasaragod'),
]

BLOOD_TYPE_CHOICES = [
    ('A+', 'A+'),
    ('A-', 'A-'),
    ('B+', 'B+'),
    ('B-', 'B-'),
    ('AB+', 'AB+'),
    ('AB-', 'AB-'),
    ('O+', 'O+'),
    ('O-', 'O-'),
]

ORGANISATION_TYPE_CHOICES = [
    ('HOSPITAL', 'Hospital'),
    ('BLOOD_BANK', 'Blood Bank'),
]

INVENTORY_STATUS_CHOICES = [
    ('AVAILABLE', 'Available'),
    ('RESERVED', 'Reserved'),
    ('DISPATCHED', 'Dispatched'),
    ('EXPIRED', 'Expired'),
    ('QUARANTINED', 'Quarantined'),
]

REQUEST_STATUS_CHOICES = [
    ('PENDING', 'Pending'),
    ('RESERVED', 'Reserved'),
    ('REQUESTED_FROM_BLOOD_BANK', 'Pending (Requested from Blood Bank)'),
    ('DISPATCHED', 'Dispatched'),
    ('FULFILLED_FROM_STOCK', 'Dispatched'),
    ('FALLBACK_DONOR_SEARCH', 'Fallback Donor Search'),
    ('REJECTED', 'Rejected'),
]

AUDIT_ACTION_CHOICES = [
    ('REQUEST_CREATED', 'Request Created'),
    ('BAG_RESERVED', 'Bag Reserved'),
    ('BANK_REQUESTED', 'Bank Requested'),
    ('BANK_REJECTED', 'Bank Rejected'),
    ('RESERVATION_LAPSED', 'Reservation Lapsed'),
    ('ISSUED', 'Issued'),
    ('FALLBACK_TRIGGERED', 'Fallback Triggered'),
    ('MANIFEST_VIEWED', 'Manifest Viewed'),
    ('DONOR_CONTACT_VIEWED', 'Donor Contact Viewed'),
    ('BAG_ADDED', 'Bag Added'),
    ('BAG_RELEASED', 'Bag Released'),
    ('BAG_EXPIRED', 'Bag Expired'),
    ('DONATION_RECORDED', 'Donation Recorded'),
    ('CSV_IMPORTED', 'CSV Imported'),
    ('REQUEST_DELETED', 'Request Deleted'),
]


class Organisation(models.Model):
    """
    Healthcare facility: Hospital or Regional Blood Bank.
    """
    name = models.CharField(max_length=255)
    org_type = models.CharField(max_length=20, choices=ORGANISATION_TYPE_CHOICES)
    district = models.CharField(max_length=50, choices=KERALA_DISTRICTS)
    address = models.TextField()
    phone = models.CharField(max_length=50)
    latitude = models.FloatField(
        validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)],
        help_text="Latitude (-90.0 to 90.0)"
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180.0), MaxValueValidator(180.0)],
        help_text="Longitude (-180.0 to 180.0)"
    )
    is_active = models.BooleanField(default=True)
    is_registered = models.BooleanField(
        default=False,
        help_text="Admin-verified; only registered organisations appear publicly."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['district', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_org_type_display()}, {self.district})"


class StaffProfile(models.Model):
    """
    Links a User to an Organisation to identify Hospital vs Blood Bank staff.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name='staff_members')

    def __str__(self):
        return f"{self.user.username} @ {self.organisation.name}"

    @property
    def is_hospital_staff(self):
        return self.organisation.org_type == 'HOSPITAL'

    @property
    def is_blood_bank_staff(self):
        return self.organisation.org_type == 'BLOOD_BANK'


class RegisteredDonor(models.Model):
    """
    Voluntary blood donor enrolled in the emergency cascade directory.
    """
    full_name = models.CharField(max_length=200)
    government_id_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="HMAC-SHA256 anonymized cryptographic hash of national or civic ID"
    )
    blood_type = models.CharField(max_length=10, choices=BLOOD_TYPE_CHOICES)
    contact_number = models.CharField(max_length=50)
    district = models.CharField(max_length=50, choices=KERALA_DISTRICTS)
    latitude = models.FloatField(
        validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)],
        help_text="Latitude (-90.0 to 90.0)"
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180.0), MaxValueValidator(180.0)],
        help_text="Longitude (-180.0 to 180.0)"
    )
    last_donated_date = models.DateField(null=True, blank=True)
    is_available_for_emergency = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()
    eligible_donors = ActiveDonorManager()

    class Meta:
        ordering = ['full_name']

    @classmethod
    def hash_government_id(cls, raw_id):
        """Hashes raw government ID using HMAC-SHA256 for privacy."""
        pepper = getattr(settings, 'DONOR_ID_PEPPER', settings.SECRET_KEY)
        return hmac.new(
            pepper.encode('utf-8'),
            raw_id.strip().encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    @property
    def cooldown_days_remaining(self):
        """Returns days remaining in the 90-day cooldown period."""
        if not self.last_donated_date:
            return 0
        today = timezone.now().date()
        cooldown_end = self.last_donated_date + timedelta(days=90)
        remaining = (cooldown_end - today).days
        return max(0, remaining)

    @property
    def is_eligible(self):
        return self.is_available_for_emergency and self.cooldown_days_remaining == 0

    @property
    def masked_contact(self):
        num = self.contact_number.strip()
        if len(num) >= 7:
            return f"{num[:3]}••••{num[-3:]}"
        return "••••••••"

    def __str__(self):
        return f"{self.full_name} ({self.blood_type}) - {self.district}"


class BloodInventory(models.Model):
    """
    Unit of blood stored in cold storage.
    """
    bag_number = models.CharField(max_length=100, unique=True)
    blood_type = models.CharField(max_length=10, choices=BLOOD_TYPE_CHOICES)
    volume_ml = models.PositiveIntegerField(default=450)
    expiry_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=INVENTORY_STATUS_CHOICES,
        default='QUARANTINED'
    )
    owner = models.ForeignKey(
        Organisation,
        on_delete=models.CASCADE,
        related_name='inventory_bags'
    )
    source_donor = models.ForeignKey(
        RegisteredDonor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='donated_bags'
    )
    reserved_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['expiry_date', 'created_at']

    def clean(self):
        super().clean()
        if self.status in ('AVAILABLE', 'RESERVED') and self.expiry_date:
            today = timezone.now().date()
            if self.expiry_date < today:
                raise ValidationError({
                    'expiry_date': f"Expiry date ({self.expiry_date}) is in the past. An expired bag cannot be marked as {self.status}."
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return bool(self.expiry_date and self.expiry_date < timezone.now().date())

    def __str__(self):
        return f"{self.bag_number} [{self.blood_type}] - {self.get_status_display()} ({self.owner.name})"


class EmergencyRequest(models.Model):
    """
    Emergency blood requisition initiated by a hospital.
    """
    requesting_hospital = models.ForeignKey(
        Organisation,
        on_delete=models.CASCADE,
        related_name='emergency_requests_made',
        limit_choices_to={'org_type': 'HOSPITAL'}
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='emergency_requests'
    )
    required_blood_type = models.CharField(max_length=10, choices=BLOOD_TYPE_CHOICES)
    patient_reference = models.CharField(
        max_length=100,
        help_text="Bed or case identifier. Do not enter real patient names or IDs."
    )
    blood_bank = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='incoming_bank_requests',
        limit_choices_to={'org_type': 'BLOOD_BANK'}
    )
    fulfilling_organisation = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fulfilled_requests'
    )
    allocated_bag = models.ForeignKey(
        BloodInventory,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='allocated_requests'
    )
    cross_match_confirmed_at = models.DateTimeField(null=True, blank=True)
    rejection_note = models.TextField(blank=True, default='')
    dispatched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=40,
        choices=REQUEST_STATUS_CHOICES,
        default='PENDING'
    )

    class Meta:
        ordering = ['-created_at']

    @property
    def target_blood_bank_name(self):
        """
        Returns the associated or nearest regional blood bank name.
        """
        if self.blood_bank:
            return self.blood_bank.name
        if self.fulfilling_organisation and self.fulfilling_organisation.org_type == 'BLOOD_BANK':
            return self.fulfilling_organisation.name
        try:
            for log in self.audit_logs.filter(action__in=['BANK_REQUESTED', 'REQUEST_CREATED']).order_by('-created_at'):
                if log.details:
                    if 'blood_bank_name' in log.details:
                        return log.details['blood_bank_name']
                    if 'routed_blood_bank' in log.details:
                        return log.details['routed_blood_bank']
                    if 'blood_bank' in log.details:
                        return log.details['blood_bank']
        except Exception:
            pass
        nearest_bank = Organisation.objects.filter(org_type='BLOOD_BANK', is_registered=True, is_active=True).first()
        return nearest_bank.name if nearest_bank else "Demo Central Blood Bank"

    def __str__(self):
        return f"REQ-{self.id:05d}: {self.requesting_hospital.name} [{self.required_blood_type}] - {self.get_status_display()}"


class AuditLog(models.Model):
    """
    Immutable audit ledger recording all blood inventory events.
    """
    actor = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs'
    )
    organisation = models.ForeignKey(
        Organisation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs'
    )
    action = models.CharField(max_length=50, choices=AUDIT_ACTION_CHOICES)
    emergency_request = models.ForeignKey(
        EmergencyRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs'
    )
    bag_number = models.CharField(max_length=100, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M:%S}] {self.action} by {self.actor or 'System'}"

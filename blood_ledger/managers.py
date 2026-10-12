from datetime import timedelta
from django.db import models
from django.db.models import Q
from django.utils import timezone


class ActiveDonorManager(models.Manager):
    """
    Manager to query active voluntary donors who are past the 90-day cooldown period.
    """

    def eligible_for_emergency(self, blood_types=None):
        today = timezone.now().date()
        cooldown_cutoff = today - timedelta(days=90)

        queryset = self.filter(
            is_available_for_emergency=True
        ).filter(
            Q(last_donated_date__isnull=True) | Q(last_donated_date__lte=cooldown_cutoff)
        )

        if blood_types is not None:
            if isinstance(blood_types, str):
                blood_types = [blood_types]
            queryset = queryset.filter(blood_type__in=blood_types)

        return queryset

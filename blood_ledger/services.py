import math
from datetime import timedelta
from functools import wraps

from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone

from .models import AuditLog, BloodInventory, EmergencyRequest

# Red-cell ABO/Rh compatibility order:
# Exact group first, compatible non-O groups next, O+ and O- kept for last.
COMPATIBILITY_MAP = {
    'AB+': ['AB+', 'AB-', 'A+', 'A-', 'B+', 'B-', 'O+', 'O-'],
    'AB-': ['AB-', 'A-', 'B-', 'O-'],
    'A+':  ['A+', 'A-', 'O+', 'O-'],
    'A-':  ['A-', 'O-'],
    'B+':  ['B+', 'B-', 'O+', 'O-'],
    'B-':  ['B-', 'O-'],
    'O+':  ['O+', 'O-'],
    'O-':  ['O-'],
}


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculates the distance in kilometers between two GPS coordinates
    using the standard Haversine formula (Earth radius R = 6371.0 km).
    """
    R = 6371.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def record_audit_log(actor, organisation, action, emergency_request=None, bag_number='', details=None):
    """
    Helper function to record an immutable audit log entry.
    """
    if details is None:
        details = {}
    return AuditLog.objects.create(
        actor=actor,
        organisation=organisation,
        action=action,
        emergency_request=emergency_request,
        bag_number=bag_number,
        details=details
    )


def reserve_matching_bag(emergency_request, organisation, actor):
    """
    Finds and reserves a matching available blood bag for 2 hours.
    Checks exact group first, then compatible alternatives.
    """
    if emergency_request.status in ('RESERVED', 'DISPATCHED', 'FULFILLED_FROM_STOCK', 'FALLBACK_DONOR_SEARCH'):
        return None

    today = timezone.now().date()
    comp_groups = COMPATIBILITY_MAP.get(emergency_request.required_blood_type, [emergency_request.required_blood_type])

    # Search compatible blood groups in priority order
    for group in comp_groups:
        candidate_bag = (
            BloodInventory.objects.select_for_update(skip_locked=True)
            .filter(
                owner=organisation,
                blood_type=group,
                status='AVAILABLE',
                expiry_date__gte=today,
            )
            .order_by('expiry_date', 'id')
            .first()
        )
        if candidate_bag:
            now = timezone.now()
            # Lock the bag for 2 hours
            candidate_bag.status = 'RESERVED'
            candidate_bag.reserved_until = now + timedelta(hours=2)
            candidate_bag.save(update_fields=['status', 'reserved_until', 'updated_at'])

            # Link bag to the emergency requisition
            emergency_request.status = 'RESERVED'
            emergency_request.fulfilling_organisation = organisation
            emergency_request.allocated_bag = candidate_bag
            emergency_request.save(update_fields=['status', 'fulfilling_organisation', 'allocated_bag'])

            is_substitute = (group != emergency_request.required_blood_type)
            record_audit_log(
                actor=actor,
                organisation=organisation,
                action='BAG_RESERVED',
                emergency_request=emergency_request,
                bag_number=candidate_bag.bag_number,
                details={
                    'requested_blood_type': emergency_request.required_blood_type,
                    'allocated_blood_type': candidate_bag.blood_type,
                    'is_substitute': is_substitute,
                    'reserved_until': candidate_bag.reserved_until.isoformat(),
                    'organisation_name': organisation.name,
                }
            )
            return candidate_bag

    return None


def rate_limit_public_page(requests_per_minute=30):
    """
    Decorator to limit public search requests to 30 requests per minute per IP.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
            if x_forwarded_for:
                ip = x_forwarded_for.split(',')[0].strip()
            else:
                ip = request.META.get('REMOTE_ADDR', '127.0.0.1')

            current_minute = int(timezone.now().timestamp() // 60)
            cache_key = f"raktagrid_ratelimit_{ip}_{current_minute}"

            try:
                count = cache.get(cache_key, 0)
                if count >= requests_per_minute:
                    return HttpResponse(
                        "Too Many Requests: Rate limit exceeded (30 requests per minute).",
                        status=429
                    )
                cache.set(cache_key, count + 1, timeout=60)
            except Exception:
                pass

            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator

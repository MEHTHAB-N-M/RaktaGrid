import csv
import io
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    BloodInventoryForm,
    CSVImportForm,
    EmergencyRequestForm,
    RecordDonationForm,
    RejectRequestForm,
)
from .models import (
    AUDIT_ACTION_CHOICES,
    BLOOD_TYPE_CHOICES,
    KERALA_DISTRICTS,
    AuditLog,
    BloodInventory,
    EmergencyRequest,
    Organisation,
    RegisteredDonor,
    StaffProfile,
)
from .services import (
    COMPATIBILITY_MAP,
    haversine_distance,
    rate_limit_public_page,
    record_audit_log,
    reserve_matching_bag,
)


def _get_staff_org(user):
    """
    Returns the organisation for the authenticated staff user, or None for superusers without a profile.
    """
    if hasattr(user, 'staff_profile'):
        return user.staff_profile.organisation
    return None


def _check_org_access(user, organisation):
    """
    Verifies user has access to data belonging to the organisation.
    Superusers have platform-wide access.
    """
    if user.is_superuser:
        return True
    staff_org = _get_staff_org(user)
    if staff_org and staff_org.id == organisation.id:
        return True
    return False


@rate_limit_public_page(requests_per_minute=30)
def public_emergency_search(request):
    """
    Zero-login public portal for citizens and responders.
    Shows registered, active hospitals and blood banks with per-group counts of AVAILABLE unexpired bags.
    Rate-limited to 30 requests/min per IP.
    """
    district_filter = request.GET.get('district', '').strip()
    blood_group_filter = request.GET.get('blood_type', '').strip()

    today = timezone.now().date()
    low_threshold = getattr(settings, 'LOW_STOCK_THRESHOLD', 4)

    orgs_qs = Organisation.objects.filter(is_registered=True, is_active=True)
    if district_filter:
        orgs_qs = orgs_qs.filter(district=district_filter)

    # Pre-calculate counts of available unexpired bags grouped by organisation and blood type
    available_bags_qs = BloodInventory.objects.filter(
        status='AVAILABLE',
        expiry_date__gte=today,
        owner__is_registered=True,
        owner__is_active=True
    )
    if district_filter:
        available_bags_qs = available_bags_qs.filter(owner__district=district_filter)

    counts_data = (
        available_bags_qs.values('owner_id', 'blood_type')
        .annotate(total=Count('id'))
    )
    # Map (org_id, blood_type) -> count
    counts_map = {(item['owner_id'], item['blood_type']): item['total'] for item in counts_data}

    org_list = []
    for org in orgs_qs:
        blood_counts = []
        for code, label in BLOOD_TYPE_CHOICES:
            if blood_group_filter and code != blood_group_filter:
                continue
            cnt = counts_map.get((org.id, code), 0)
            if cnt >= low_threshold:
                status_color = 'success'
                status_text = 'In Stock'
            elif cnt > 0:
                status_color = 'warning'
                status_text = 'Low Stock'
            else:
                status_color = 'danger'
                status_text = 'Empty'
            blood_counts.append({
                'code': code,
                'count': cnt,
                'status_color': status_color,
                'status_text': status_text,
            })

        org_list.append({
            'org': org,
            'blood_counts': blood_counts,
        })

    context = {
        'districts': [d[0] for d in KERALA_DISTRICTS],
        'blood_types': [b[0] for b in BLOOD_TYPE_CHOICES],
        'selected_district': district_filter,
        'selected_blood_type': blood_group_filter,
        'org_list': org_list,
        'low_threshold': low_threshold,
    }
    return render(request, 'blood_ledger/public_emergency_search.html', context)


@login_required
def dashboard_view(request):
    """
    Role-scoped dashboard:
    - Hospital staff: Own emergency requests, own stock summary, request creation.
    - Blood bank staff: Incoming requests addressed to blood bank, fulfillments, inventory breakdown.
    - Superuser: System overview.
    """
    user = request.user
    staff_org = _get_staff_org(user)
    today = timezone.now().date()

    if not staff_org and not user.is_superuser:
        messages.error(request, "Your account is not linked to any organisation. Contact platform administrator.")
        return redirect('public_search')

    context = {
        'staff_org': staff_org,
        'today': today,
    }

    if user.is_superuser:
        context['is_admin'] = True
        context['requests'] = EmergencyRequest.objects.all().select_related('requesting_hospital', 'blood_bank', 'allocated_bag')[:20]
        context['total_orgs'] = Organisation.objects.count()
        context['total_bags'] = BloodInventory.objects.count()
        context['available_bags'] = BloodInventory.objects.filter(status='AVAILABLE', expiry_date__gte=today).count()
        context['pending_requests'] = EmergencyRequest.objects.filter(status='PENDING').count()
        return render(request, 'blood_ledger/dashboard.html', context)

    if staff_org.org_type == 'HOSPITAL':
        # Hospital Staff View
        context['is_hospital'] = True
        context['my_requests'] = EmergencyRequest.objects.filter(
            requesting_hospital=staff_org
        ).select_related('allocated_bag', 'blood_bank', 'fulfilling_organisation')
        context['create_form'] = EmergencyRequestForm()

        # Own hospital inventory
        own_bags = BloodInventory.objects.filter(owner=staff_org)
        context['available_bags_count'] = own_bags.filter(status='AVAILABLE', expiry_date__gte=today).count()
        context['reserved_bags_count'] = own_bags.filter(status='RESERVED').count()
        context['quarantined_bags_count'] = own_bags.filter(status='QUARANTINED').count()
        context['dispatched_bags_count'] = own_bags.filter(status='DISPATCHED').count()
        context['recent_bags'] = own_bags.order_by('-created_at')[:10]

    else:
        # Blood Bank Staff View
        context['is_blood_bank'] = True
        context['incoming_requests'] = EmergencyRequest.objects.filter(
            Q(blood_bank=staff_org) | Q(blood_bank__isnull=True),
            status__in=['PENDING', 'REQUESTED_FROM_BLOOD_BANK']
        ).select_related('requesting_hospital', 'blood_bank')

        if not context['incoming_requests'].exists():
            context['incoming_requests'] = EmergencyRequest.objects.filter(
                status__in=['PENDING', 'REQUESTED_FROM_BLOOD_BANK']
            ).select_related('requesting_hospital', 'blood_bank')

        context['fulfilling_requests'] = EmergencyRequest.objects.filter(
            Q(fulfilling_organisation=staff_org) | Q(blood_bank=staff_org),
            status='RESERVED'
        ).select_related('requesting_hospital', 'allocated_bag')

        # Own blood bank inventory
        bank_bags = BloodInventory.objects.filter(owner=staff_org)
        context['available_bags_count'] = bank_bags.filter(status='AVAILABLE', expiry_date__gte=today).count()
        context['reserved_bags_count'] = bank_bags.filter(status='RESERVED').count()
        context['quarantined_bags_count'] = bank_bags.filter(status='QUARANTINED').count()
        context['dispatched_bags_count'] = bank_bags.filter(status='DISPATCHED').count()
        context['recent_bags'] = bank_bags.order_by('-created_at')[:15]

    return render(request, 'blood_ledger/dashboard.html', context)


@login_required
def create_emergency_request_view(request):
    """
    Step 1: Hospital staff creates an emergency request for their hospital.
    """
    staff_org = _get_staff_org(request.user)
    if not staff_org or staff_org.org_type != 'HOSPITAL':
        raise PermissionDenied("Only hospital staff can create emergency requisitions.")

    today = timezone.now().date()

    if request.method == 'POST':
        form = EmergencyRequestForm(request.POST, hospital=staff_org)
        if form.is_valid():
            with transaction.atomic():
                req = form.save(commit=False)
                req.requesting_hospital = staff_org
                req.requested_by = request.user

                selected_bank = form.cleaned_data.get('blood_bank')
                if not selected_bank:
                    banks = list(Organisation.objects.filter(org_type='BLOOD_BANK', is_registered=True, is_active=True))
                    if banks:
                        banks.sort(key=lambda b: haversine_distance(staff_org.latitude, staff_org.longitude, b.latitude, b.longitude))
                        selected_bank = banks[0]

                if selected_bank:
                    req.blood_bank = selected_bank
                    req.status = 'PENDING'
                    req.save()

                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='REQUEST_CREATED',
                        emergency_request=req,
                        details={
                            'required_blood_type': req.required_blood_type,
                            'patient_reference': req.patient_reference,
                            'routed_blood_bank': selected_bank.name,
                        }
                    )
                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='BANK_REQUESTED',
                        emergency_request=req,
                        details={
                            'blood_bank': selected_bank.name,
                            'district': selected_bank.district,
                            'required_blood_type': req.required_blood_type,
                            'direct_route_from_form': True,
                        }
                    )
                    messages.success(
                        request,
                        f"Emergency Requisition #REQ-{req.id:05d} created with status PENDING and requested to {selected_bank.name}."
                    )
                else:
                    req.status = 'PENDING'
                    req.save()

                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='REQUEST_CREATED',
                        emergency_request=req,
                        details={
                            'required_blood_type': req.required_blood_type,
                            'patient_reference': req.patient_reference,
                            'mode': 'Check Own Hospital Inventory First',
                        }
                    )
                    messages.success(request, f"Emergency Requisition #REQ-{req.id:05d} created successfully.")

            return redirect('request_detail', request_id=req.id)
    else:
        initial_data = {}
        if 'bank_id' in request.GET:
            try:
                initial_data['blood_bank'] = int(request.GET['bank_id'])
            except (ValueError, TypeError):
                pass
        form = EmergencyRequestForm(initial=initial_data, hospital=staff_org)

    # Calculate regional blood bank live availability matrix
    banks = Organisation.objects.filter(org_type='BLOOD_BANK', is_registered=True, is_active=True)
    blood_types = [bt[0] for bt in BLOOD_TYPE_CHOICES]
    bank_availability = []

    for bank in banks:
        dist = haversine_distance(staff_org.latitude, staff_org.longitude, bank.latitude, bank.longitude)
        avail_bags = BloodInventory.objects.filter(
            owner=bank,
            status='AVAILABLE',
            expiry_date__gte=today
        )
        counts = {bt: 0 for bt in blood_types}
        for item in avail_bags.values('blood_type').annotate(cnt=Count('id')):
            counts[item['blood_type']] = item['cnt']
        total_avail = avail_bags.count()

        bank_availability.append({
            'bank': bank,
            'distance_km': round(dist, 1),
            'counts': counts,
            'total_available': total_avail,
        })

    bank_availability.sort(key=lambda x: x['distance_km'])

    # Own hospital available stock for comparison
    own_bags = BloodInventory.objects.filter(owner=staff_org, status='AVAILABLE', expiry_date__gte=today)
    own_counts = {bt: 0 for bt in blood_types}
    for item in own_bags.values('blood_type').annotate(cnt=Count('id')):
        own_counts[item['blood_type']] = item['cnt']
    own_total = own_bags.count()

    context = {
        'form': form,
        'staff_org': staff_org,
        'bank_availability': bank_availability,
        'blood_types': blood_types,
        'own_counts': own_counts,
        'own_total': own_total,
    }
    return render(request, 'blood_ledger/request_create.html', context)


@login_required
def request_detail_view(request, request_id):
    """
    Organisation-scoped request detail view and coordination center.
    """
    emergency_request = get_object_or_404(
        EmergencyRequest.objects.select_related(
            'requesting_hospital', 'blood_bank', 'fulfilling_organisation', 'allocated_bag'
        ),
        id=request_id
    )

    staff_org = _get_staff_org(request.user)
    is_admin = request.user.is_superuser
    is_requesting = bool(staff_org and emergency_request.requesting_hospital_id == staff_org.id)
    is_assigned_bank = bool(staff_org and emergency_request.blood_bank_id == staff_org.id)
    is_fulfilling = bool(staff_org and emergency_request.fulfilling_organisation_id == staff_org.id)
    is_blood_bank = bool(staff_org and staff_org.org_type == 'BLOOD_BANK')

    if not (is_admin or is_requesting or is_assigned_bank or is_fulfilling or is_blood_bank):
        raise PermissionDenied("You do not have access to this emergency request.")

    today = timezone.now().date()
    matching_blood_banks = []

    # If PENDING and hospital staff viewing: evaluate Step 3 (blood banks with matching stock)
    if emergency_request.status == 'PENDING' and (is_requesting or is_admin):
        comp_groups = COMPATIBILITY_MAP.get(emergency_request.required_blood_type, [emergency_request.required_blood_type])
        hosp_lat = emergency_request.requesting_hospital.latitude
        hosp_lon = emergency_request.requesting_hospital.longitude

        # Find registered active blood banks with matching stock
        bank_ids_with_stock = (
            BloodInventory.objects.filter(
                owner__org_type='BLOOD_BANK',
                owner__is_registered=True,
                owner__is_active=True,
                blood_type__in=comp_groups,
                status='AVAILABLE',
                expiry_date__gte=today
            )
            .values_list('owner_id', flat=True)
            .distinct()
        )

        for bank in Organisation.objects.filter(id__in=bank_ids_with_stock):
            dist = haversine_distance(hosp_lat, hosp_lon, bank.latitude, bank.longitude)
            # Count exact vs compatible stock
            exact_count = BloodInventory.objects.filter(
                owner=bank, blood_type=emergency_request.required_blood_type, status='AVAILABLE', expiry_date__gte=today
            ).count()
            comp_count = BloodInventory.objects.filter(
                owner=bank, blood_type__in=comp_groups, status='AVAILABLE', expiry_date__gte=today
            ).exclude(blood_type=emergency_request.required_blood_type).count()

            matching_blood_banks.append({
                'bank': bank,
                'distance_km': round(dist, 1),
                'exact_count': exact_count,
                'comp_count': comp_count,
                'total_count': exact_count + comp_count,
            })

        matching_blood_banks.sort(key=lambda x: x['distance_km'])

    context = {
        'req': emergency_request,
        'staff_org': staff_org,
        'is_requesting': is_requesting,
        'is_assigned_bank': is_assigned_bank,
        'is_fulfilling': is_fulfilling,
        'is_blood_bank': is_blood_bank,
        'is_admin': is_admin,
        'matching_blood_banks': matching_blood_banks,
        'reject_form': RejectRequestForm(),
    }
    return render(request, 'blood_ledger/request_detail.html', context)


@login_required
def check_own_stock_view(request, request_id):
    """
    Step 2: Check own stock inside transaction.atomic with select_for_update.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or (staff_org and emergency_request.requesting_hospital_id == staff_org.id)):
            raise PermissionDenied("Only the requesting hospital can check its own inventory.")

        if emergency_request.status != 'PENDING':
            messages.warning(request, f"Request is currently {emergency_request.get_status_display()} and cannot reserve from stock.")
            return redirect('request_detail', request_id=request_id)

        reserved_bag = reserve_matching_bag(emergency_request, emergency_request.requesting_hospital, request.user)
        if reserved_bag:
            messages.success(
                request,
                f"Successfully reserved Bag #{reserved_bag.bag_number} ({reserved_bag.blood_type}) from hospital inventory! Issue blood once cross-match is verified."
            )
        else:
            messages.info(
                request,
                f"No available {emergency_request.required_blood_type} stock in hospital inventory. You may now request from a nearby blood bank."
            )

    return redirect('request_detail', request_id=request_id)


@login_required
def request_blood_bank_view(request, request_id):
    """
    Step 3: Hospital requests matching stock from a chosen blood bank.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    bank_id = request.POST.get('blood_bank_id')
    blood_bank = get_object_or_404(Organisation, id=bank_id, org_type='BLOOD_BANK', is_registered=True, is_active=True)

    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or (staff_org and emergency_request.requesting_hospital_id == staff_org.id)):
            raise PermissionDenied("Only the requesting hospital can dispatch bank requests.")

        if emergency_request.status != 'PENDING':
            messages.warning(request, "Request cannot be routed to a blood bank in its current status.")
            return redirect('request_detail', request_id=request_id)

        emergency_request.status = 'PENDING'
        emergency_request.blood_bank = blood_bank
        emergency_request.save(update_fields=['status', 'blood_bank'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='BANK_REQUESTED',
            emergency_request=emergency_request,
            details={
                'blood_bank_id': blood_bank.id,
                'blood_bank_name': blood_bank.name,
                'required_blood_type': emergency_request.required_blood_type,
            }
        )

    messages.success(request, f"Requisition dispatched to {blood_bank.name}. Awaiting blood bank confirmation.")
    return redirect('request_detail', request_id=request_id)


@login_required
def accept_bank_request_view(request, request_id):
    """
    Step 4 (Accept): Blood bank staff accept and reserve from their stock.
    If stock is gone, reject automatically with a clear message.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    is_blood_bank = bool(staff_org and staff_org.org_type == 'BLOOD_BANK')

    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or is_blood_bank or (staff_org and emergency_request.blood_bank_id == staff_org.id)):
            raise PermissionDenied("Only a blood bank can accept this request.")

        if emergency_request.status not in ('PENDING', 'REQUESTED_FROM_BLOOD_BANK'):
            messages.warning(request, f"Request is not awaiting blood bank review (Status: {emergency_request.status}).")
            return redirect('request_detail', request_id=request_id)

        if staff_org and emergency_request.blood_bank_id != staff_org.id:
            emergency_request.blood_bank = staff_org
            emergency_request.save(update_fields=['blood_bank'])

        reserved_bag = reserve_matching_bag(emergency_request, staff_org, request.user)
        if reserved_bag:
            messages.success(
                request,
                f"Accepted & Reserved: Bag #{reserved_bag.bag_number} ({reserved_bag.blood_type}) reserved for {emergency_request.requesting_hospital.name}."
            )
        else:
            # Stock is depleted: auto-reject back to PENDING
            emergency_request.status = 'PENDING'
            emergency_request.rejection_note = f"Automatic rejection: Inventory at {staff_org.name} was depleted before reservation could lock."
            assigned_bank = emergency_request.blood_bank
            emergency_request.blood_bank = None
            emergency_request.save(update_fields=['status', 'rejection_note', 'blood_bank'])

            record_audit_log(
                actor=request.user,
                organisation=staff_org,
                action='BANK_REJECTED',
                emergency_request=emergency_request,
                details={
                    'reason': emergency_request.rejection_note,
                    'auto_rejected': True,
                }
            )
            messages.error(
                request,
                "Stock was no longer available. Request automatically rejected and returned to PENDING for alternative routing."
            )

    return redirect('request_detail', request_id=request_id)


@login_required
def reject_bank_request_view(request, request_id):
    """
    Step 4 (Reject): Blood bank staff manually reject with a required reason.
    Returns request to PENDING with rejection_note.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    is_blood_bank = bool(staff_org and staff_org.org_type == 'BLOOD_BANK')
    form = RejectRequestForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A clinical rejection reason is mandatory.")
        return redirect('request_detail', request_id=request_id)

    reason = form.cleaned_data['rejection_note']

    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or is_blood_bank or (staff_org and emergency_request.blood_bank_id == staff_org.id)):
            raise PermissionDenied("Only a blood bank can reject this request.")

        if emergency_request.status not in ('PENDING', 'REQUESTED_FROM_BLOOD_BANK'):
            messages.warning(request, "Request cannot be rejected in its current status.")
            return redirect('request_detail', request_id=request_id)

        emergency_request.status = 'PENDING'
        emergency_request.rejection_note = reason
        emergency_request.blood_bank = None
        emergency_request.save(update_fields=['status', 'rejection_note', 'blood_bank'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='BANK_REJECTED',
            emergency_request=emergency_request,
            details={'reason': reason}
        )

    messages.info(request, "Request rejected and returned to PENDING. Requesting hospital may try another facility.")
    return redirect('request_detail', request_id=request_id)


@login_required
def issue_blood_view(request, request_id):
    """
    Step 5 (Issue): Fulfilling organisation's staff ticks 'Cross-match completed' and clicks 'Issue blood'.
    Sets cross_match_confirmed_at, bag DISPATCHED, request FULFILLED_FROM_STOCK, dispatched_at.
    Redirects to dispatch manifest.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    is_blood_bank = bool(staff_org and staff_org.org_type == 'BLOOD_BANK')
    cross_match_tick = request.POST.get('cross_match_confirmed') == '1'

    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )

        is_fulfilling = bool(staff_org and emergency_request.fulfilling_organisation_id == staff_org.id)
        is_assigned_bank = bool(staff_org and emergency_request.blood_bank_id == staff_org.id)
        if not (request.user.is_superuser or is_fulfilling or is_assigned_bank or is_blood_bank):
            raise PermissionDenied("Only the fulfilling organisation can issue this blood unit.")

        # Refusal check: must be RESERVED, have an allocated bag that is RESERVED for this request, and cross-match ticked
        if emergency_request.status != 'RESERVED' or not emergency_request.allocated_bag:
            messages.error(request, "Issue refused: Blood unit is not reserved for this request.")
            return redirect('request_detail', request_id=request_id)

        if emergency_request.allocated_bag.status != 'RESERVED':
            messages.error(request, "Issue refused: Allocated bag is not in RESERVED status.")
            return redirect('request_detail', request_id=request_id)

        if not cross_match_tick:
            messages.error(request, "Issue refused: Clinical cross-match completion checkbox must be checked prior to dispatch.")
            return redirect('request_detail', request_id=request_id)

        now = timezone.now()
        bag = emergency_request.allocated_bag
        bag.status = 'DISPATCHED'
        bag.save(update_fields=['status', 'updated_at'])

        emergency_request.cross_match_confirmed_at = now
        emergency_request.dispatched_at = now
        emergency_request.status = 'DISPATCHED'
        emergency_request.save(update_fields=['cross_match_confirmed_at', 'dispatched_at', 'status'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='ISSUED',
            emergency_request=emergency_request,
            bag_number=bag.bag_number,
            details={
                'cross_match_confirmed_at': now.isoformat(),
                'dispatched_at': now.isoformat(),
                'issued_blood_type': bag.blood_type,
                'requested_blood_type': emergency_request.required_blood_type,
            }
        )

    messages.success(request, f"Blood unit #{bag.bag_number} successfully issued and dispatched!")
    return redirect('dispatch_manifest', request_id=emergency_request.id)


@login_required
def direct_dispatch_bank_request_view(request, request_id):
    """
    Direct dispatch action by designated blood bank staff:
    Atomically allocates/reserves matching stock, verifies cross-match,
    sets bag and requisition status to DISPATCHED, and redirects to dispatch manifest.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    is_blood_bank = bool(staff_org and staff_org.org_type == 'BLOOD_BANK')
    cross_match_tick = request.POST.get('cross_match_confirmed') == '1'

    if not cross_match_tick:
        messages.error(request, "Issue refused: Clinical cross-match completion checkbox must be checked prior to dispatch.")
        return redirect('request_detail', request_id=request_id)

    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or is_blood_bank or (staff_org and emergency_request.blood_bank_id == staff_org.id)):
            raise PermissionDenied("Only a blood bank can dispatch this request.")

        if emergency_request.status not in ('PENDING', 'REQUESTED_FROM_BLOOD_BANK'):
            messages.warning(request, f"Request cannot be dispatched in its current status ({emergency_request.status}).")
            return redirect('request_detail', request_id=request_id)

        if staff_org and emergency_request.blood_bank_id != staff_org.id:
            emergency_request.blood_bank = staff_org
            emergency_request.save(update_fields=['blood_bank'])

        reserved_bag = reserve_matching_bag(emergency_request, staff_org, request.user)
        if not reserved_bag:
            messages.error(request, "Stock was no longer available. Unable to dispatch.")
            return redirect('request_detail', request_id=request_id)

        now = timezone.now()
        reserved_bag.status = 'DISPATCHED'
        reserved_bag.save(update_fields=['status', 'updated_at'])

        emergency_request.cross_match_confirmed_at = now
        emergency_request.dispatched_at = now
        emergency_request.status = 'DISPATCHED'
        emergency_request.fulfilling_organisation = staff_org
        emergency_request.save(update_fields=['cross_match_confirmed_at', 'dispatched_at', 'status', 'fulfilling_organisation'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='ISSUED',
            emergency_request=emergency_request,
            bag_number=reserved_bag.bag_number,
            details={
                'cross_match_confirmed_at': now.isoformat(),
                'dispatched_at': now.isoformat(),
                'issued_blood_type': reserved_bag.blood_type,
                'requested_blood_type': emergency_request.required_blood_type,
                'direct_dispatch': True,
            }
        )

    messages.success(request, f"Blood unit #{reserved_bag.bag_number} successfully verified and dispatched to {emergency_request.requesting_hospital.name}!")
    return redirect('dispatch_manifest', request_id=emergency_request.id)


@login_required
def trigger_fallback_view(request, request_id):
    """
    Step 6: Initiate voluntary donor fallback when no blood bank has matching stock.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )
        if not (request.user.is_superuser or (staff_org and emergency_request.requesting_hospital_id == staff_org.id)):
            raise PermissionDenied("Only the requesting hospital can activate the donor fallback cascade.")

        if emergency_request.status in ('RESERVED', 'FULFILLED_FROM_STOCK'):
            messages.warning(request, "Request already has reserved or fulfilled units.")
            return redirect('request_detail', request_id=request_id)

        emergency_request.status = 'FALLBACK_DONOR_SEARCH'
        emergency_request.save(update_fields=['status'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='FALLBACK_TRIGGERED',
            emergency_request=emergency_request,
            details={'reason': 'No blood bank holding matching stock'}
        )

    messages.info(request, "Voluntary donor fallback initiated. Showing nearest medically eligible donors.")
    return redirect('donor_directory', request_id=request_id)


@login_required
def delete_emergency_request_view(request, request_id):
    """
    Deletes an emergency requisition.
    Allowed for staff of the requesting hospital or platform administrators.
    If a unit was reserved, it is atomically released back to AVAILABLE cold storage.
    Fulfilled requests with a generated dispatch manifest cannot be deleted by non-admins.
    """
    if request.method != 'POST':
        return redirect('request_detail', request_id=request_id)

    staff_org = _get_staff_org(request.user)
    with transaction.atomic():
        emergency_request = get_object_or_404(
            EmergencyRequest.objects.select_for_update(),
            id=request_id
        )

        is_admin = request.user.is_superuser
        is_requesting = bool(staff_org and emergency_request.requesting_hospital_id == staff_org.id)

        if not (is_admin or is_requesting):
            raise PermissionDenied("Only the requesting hospital or platform administrator can delete this requisition.")

        if emergency_request.status in ('DISPATCHED', 'FULFILLED_FROM_STOCK') and not is_admin:
            messages.error(
                request,
                "Cannot delete a fulfilled emergency requisition. A blood unit has already been physically dispatched."
            )
            return redirect('request_detail', request_id=request_id)

        # If a bag was reserved, release it back to AVAILABLE status
        if emergency_request.allocated_bag:
            bag = BloodInventory.objects.select_for_update().filter(id=emergency_request.allocated_bag_id).first()
            if bag and bag.status == 'RESERVED':
                bag.status = 'AVAILABLE'
                bag.reserved_until = None
                bag.save(update_fields=['status', 'reserved_until', 'updated_at'])

                record_audit_log(
                    actor=request.user,
                    organisation=staff_org or emergency_request.requesting_hospital,
                    action='BAG_RELEASED',
                    emergency_request=None,
                    bag_number=bag.bag_number,
                    details={
                        'reason': f"Requisition #REQ-{emergency_request.id:05d} deleted. Reserved unit released back to stock.",
                        'previous_status': 'RESERVED',
                    }
                )

        req_ref = f"REQ-{emergency_request.id:05d}"

        # Record audit ledger entry
        record_audit_log(
            actor=request.user,
            organisation=staff_org or emergency_request.requesting_hospital,
            action='REQUEST_DELETED',
            emergency_request=None,
            details={
                'deleted_request_id': emergency_request.id,
                'request_ref': req_ref,
                'hospital': emergency_request.requesting_hospital.name,
                'patient_reference': emergency_request.patient_reference,
                'required_blood_type': emergency_request.required_blood_type,
                'status_at_deletion': emergency_request.status,
            }
        )

        emergency_request.delete()

    messages.success(request, f"Emergency Requisition #{req_ref} has been deleted.")
    return redirect('dashboard')


@login_required
def donor_directory_view(request, request_id):
    """
    Voluntary donor directory for emergency fallback:
    - Filters medically eligible donors using ActiveDonorManager.
    - Haversine distance from requesting hospital coordinates.
    - Filters within DONOR_SEARCH_RADIUS_KM (default 50 km).
    - Sorts exact-group donors before compatible ones (with 'Compatible group' badge), ordered by distance ASC.
    - Masked phone numbers by default.
    """
    emergency_request = get_object_or_404(
        EmergencyRequest.objects.select_related('requesting_hospital'),
        id=request_id
    )
    staff_org = _get_staff_org(request.user)
    if not (request.user.is_superuser or (staff_org and emergency_request.requesting_hospital_id == staff_org.id)):
        raise PermissionDenied("Access restricted to requesting hospital staff.")

    radius_km = getattr(settings, 'DONOR_SEARCH_RADIUS_KM', 50)
    hosp_lat = emergency_request.requesting_hospital.latitude
    hosp_lon = emergency_request.requesting_hospital.longitude

    comp_groups = COMPATIBILITY_MAP.get(emergency_request.required_blood_type, [emergency_request.required_blood_type])

    # Fetch all eligible donors across exact and compatible groups
    donors_qs = RegisteredDonor.eligible_donors.eligible_for_emergency(blood_types=comp_groups)

    exact_donors = []
    compatible_donors = []

    revealed_ids = request.session.get('revealed_donors', [])

    for donor in donors_qs:
        dist = haversine_distance(hosp_lat, hosp_lon, donor.latitude, donor.longitude)
        if dist <= radius_km:
            donor.distance_km = round(dist, 1)
            donor.is_revealed = (donor.id in revealed_ids)
            if donor.blood_type == emergency_request.required_blood_type:
                donor.is_compatible_substitute = False
                exact_donors.append(donor)
            else:
                donor.is_compatible_substitute = True
                compatible_donors.append(donor)

    # Sort each list by proximity (nearest first)
    exact_donors.sort(key=lambda d: d.distance_km)
    compatible_donors.sort(key=lambda d: d.distance_km)

    all_eligible_donors = exact_donors + compatible_donors

    context = {
        'req': emergency_request,
        'donors': all_eligible_donors,
        'radius_km': radius_km,
        'total_count': len(all_eligible_donors),
    }
    return render(request, 'blood_ledger/donor_directory.html', context)


@login_required
def reveal_donor_contact_view(request, request_id, donor_id):
    """
    POST action by staff of requesting hospital to unmask donor phone number.
    Writes DONOR_CONTACT_VIEWED audit entry.
    """
    if request.method != 'POST':
        return redirect('donor_directory', request_id=request_id)

    emergency_request = get_object_or_404(EmergencyRequest, id=request_id)
    staff_org = _get_staff_org(request.user)
    if not (request.user.is_superuser or (staff_org and emergency_request.requesting_hospital_id == staff_org.id)):
        raise PermissionDenied("Only the requesting hospital can view donor contact details.")

    donor = get_object_or_404(RegisteredDonor, id=donor_id)

    with transaction.atomic():
        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='DONOR_CONTACT_VIEWED',
            emergency_request=emergency_request,
            details={
                'donor_id': donor.id,
                'donor_name': donor.full_name,
                'donor_blood_type': donor.blood_type,
            }
        )

    # Store revealed state in session
    revealed_list = request.session.get('revealed_donors', [])
    if donor.id not in revealed_list:
        revealed_list.append(donor.id)
        request.session['revealed_donors'] = revealed_list
        request.session.modified = True

    messages.info(request, f"Contact details revealed for {donor.full_name}.")
    return redirect('donor_directory', request_id=request_id)


@login_required
def dispatch_manifest_view(request, request_id):
    """
    Print-friendly HTML Dispatch Manifest (Section 7).
    Restricted to requesting hospital, fulfilling organisation, and platform admins.
    Audits MANIFEST_VIEWED.
    """
    emergency_request = get_object_or_404(
        EmergencyRequest.objects.select_related(
            'requesting_hospital', 'fulfilling_organisation', 'allocated_bag'
        ),
        id=request_id
    )

    staff_org = _get_staff_org(request.user)
    is_admin = request.user.is_superuser
    is_requesting = bool(staff_org and emergency_request.requesting_hospital_id == staff_org.id)
    is_fulfilling = bool(staff_org and emergency_request.fulfilling_organisation_id == staff_org.id)

    if not (is_admin or is_requesting or is_fulfilling):
        raise PermissionDenied("Access restricted to participating organisations.")

    # Record manifest view
    with transaction.atomic():
        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='MANIFEST_VIEWED',
            emergency_request=emergency_request,
            bag_number=emergency_request.allocated_bag.bag_number if emergency_request.allocated_bag else '',
            details={'manifest_viewed_at': timezone.now().isoformat()}
        )

    is_substitute = (
        emergency_request.allocated_bag
        and emergency_request.allocated_bag.blood_type != emergency_request.required_blood_type
    )

    context = {
        'req': emergency_request,
        'bag': emergency_request.allocated_bag,
        'is_substitute': is_substitute,
    }
    return render(request, 'blood_ledger/dispatch_manifest.html', context)


@login_required
def add_bag_view(request):
    """
    Add Bag form: staff adds a bag to their organisation.
    New bags default to QUARANTINED.
    """
    staff_org = _get_staff_org(request.user)
    if not staff_org and not request.user.is_superuser:
        raise PermissionDenied("Must belong to an organisation to add inventory.")

    if request.method == 'POST':
        form = BloodInventoryForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                bag = form.save(commit=False)
                bag.owner = staff_org
                bag.status = 'QUARANTINED'
                bag.save()

                record_audit_log(
                    actor=request.user,
                    organisation=staff_org,
                    action='BAG_ADDED',
                    bag_number=bag.bag_number,
                    details={
                        'blood_type': bag.blood_type,
                        'volume_ml': bag.volume_ml,
                        'expiry_date': str(bag.expiry_date),
                        'status': bag.status,
                    }
                )
            messages.success(request, f"Bag #{bag.bag_number} registered in QUARANTINED status. Release to stock once safety tests pass.")
            return redirect('dashboard')
    else:
        form = BloodInventoryForm()

    return render(request, 'blood_ledger/bag_add.html', {'form': form, 'staff_org': staff_org})


@login_required
def release_bag_view(request, bag_id):
    """
    Moves QUARANTINED bag to AVAILABLE (owning organisation only).
    """
    if request.method != 'POST':
        return redirect('dashboard')

    bag = get_object_or_404(BloodInventory, id=bag_id)
    staff_org = _get_staff_org(request.user)

    if not (request.user.is_superuser or (staff_org and bag.owner_id == staff_org.id)):
        raise PermissionDenied("Only the owning organisation can release bags.")

    if bag.status != 'QUARANTINED':
        messages.warning(request, f"Bag #{bag.bag_number} is not quarantined.")
        return redirect('dashboard')

    if bag.is_expired:
        messages.error(request, f"Cannot release bag #{bag.bag_number}: expiry date ({bag.expiry_date}) has passed.")
        return redirect('dashboard')

    with transaction.atomic():
        bag.status = 'AVAILABLE'
        bag.save(update_fields=['status', 'updated_at'])

        record_audit_log(
            actor=request.user,
            organisation=staff_org,
            action='BAG_RELEASED',
            bag_number=bag.bag_number,
            details={'previous_status': 'QUARANTINED', 'new_status': 'AVAILABLE'}
        )

    messages.success(request, f"Bag #{bag.bag_number} released to cold storage (AVAILABLE).")
    return redirect('dashboard')


@login_required
def record_donation_view(request):
    """
    Section 4A: Record Donation (blood bank staff only).
    Pick donor, enter bag details. Reject if donor in cooldown.
    In one atomic block: create QUARANTINED bag, update donor's last_donated_date to today, write DONATION_RECORDED.
    """
    staff_org = _get_staff_org(request.user)
    if not (request.user.is_superuser or (staff_org and staff_org.org_type == 'BLOOD_BANK')):
        raise PermissionDenied("Only blood bank staff can record whole blood donations.")

    if request.method == 'POST':
        form = RecordDonationForm(request.POST)
        if form.is_valid():
            donor = form.cleaned_data['donor']
            bag_num = form.cleaned_data['bag_number']
            vol = form.cleaned_data['volume_ml']
            exp = form.cleaned_data['expiry_date']

            # Double check uniqueness
            if BloodInventory.objects.filter(bag_number=bag_num).exists():
                form.add_error('bag_number', f"A bag with number {bag_num} already exists.")
            else:
                today = timezone.now().date()
                with transaction.atomic():
                    bag = BloodInventory.objects.create(
                        bag_number=bag_num,
                        blood_type=donor.blood_type,
                        volume_ml=vol,
                        expiry_date=exp,
                        status='QUARANTINED',
                        owner=staff_org,
                        source_donor=donor
                    )
                    donor.last_donated_date = today
                    donor.save(update_fields=['last_donated_date'])

                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='DONATION_RECORDED',
                        bag_number=bag.bag_number,
                        details={
                            'donor_id': donor.id,
                            'donor_name': donor.full_name,
                            'blood_type': bag.blood_type,
                            'donation_date': str(today),
                        }
                    )

                messages.success(request, f"Donation recorded: Bag #{bag.bag_number} created as QUARANTINED. Donor cooldown reset to 90 days.")
                return redirect('dashboard')
    else:
        form = RecordDonationForm()

    return render(request, 'blood_ledger/record_donation.html', {'form': form, 'staff_org': staff_org})


@login_required
def csv_import_bags_view(request):
    """
    Section 4E: CSV import of blood bags.
    Columns: bag_number, blood_type, volume_ml, expiry_date, status
    Validate every row first; import everything in one transaction only if all rows valid.
    """
    staff_org = _get_staff_org(request.user)
    if not staff_org and not request.user.is_superuser:
        raise PermissionDenied("Must belong to an organisation to import bags.")

    errors = []
    if request.method == 'POST':
        form = CSVImportForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = form.cleaned_data['csv_file']
            try:
                decoded_file = csv_file.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                errors.append("File encoding error: CSV must be encoded in UTF-8.")
                return render(request, 'blood_ledger/csv_import.html', {'form': form, 'import_type': 'bags', 'errors': errors})

            reader = csv.DictReader(io.StringIO(decoded_file))
            expected_fields = {'bag_number', 'blood_type', 'volume_ml', 'expiry_date', 'status'}
            if not expected_fields.issubset(set(f.strip() for f in (reader.fieldnames or []))):
                errors.append(f"Header error: CSV must contain columns: {', '.join(sorted(expected_fields))}")
                return render(request, 'blood_ledger/csv_import.html', {'form': form, 'import_type': 'bags', 'errors': errors})

            valid_blood_types = {b[0] for b in BLOOD_TYPE_CHOICES}
            valid_statuses = {'AVAILABLE', 'RESERVED', 'DISPATCHED', 'EXPIRED', 'QUARANTINED'}
            today = timezone.now().date()

            validated_rows = []
            seen_bag_numbers = set()

            for line_no, row in enumerate(reader, start=2):
                bag_no = row.get('bag_number', '').strip()
                b_type = row.get('blood_type', '').strip()
                vol_str = row.get('volume_ml', '').strip()
                exp_str = row.get('expiry_date', '').strip()
                status = row.get('status', '').strip().upper()

                # Required checks
                if not bag_no or not b_type or not vol_str or not exp_str or not status:
                    errors.append(f"Row {line_no}: Missing required field values.")
                    continue

                if bag_no in seen_bag_numbers or BloodInventory.objects.filter(bag_number=bag_no).exists():
                    errors.append(f"Row {line_no}: Duplicate bag number '{bag_no}'.")
                    continue
                seen_bag_numbers.add(bag_no)

                if b_type not in valid_blood_types:
                    errors.append(f"Row {line_no}: Invalid blood type '{b_type}'. Allowed: {', '.join(sorted(valid_blood_types))}.")

                try:
                    vol = int(vol_str)
                    if vol <= 0 or vol > 1000:
                        errors.append(f"Row {line_no}: Volume '{vol_str}' out of realistic range (1-1000 ml).")
                except ValueError:
                    errors.append(f"Row {line_no}: Volume '{vol_str}' is not an integer.")
                    vol = None

                try:
                    exp_date = datetime.strptime(exp_str, '%Y-%m-%d').date()
                except ValueError:
                    errors.append(f"Row {line_no}: Expiry date '{exp_str}' must be in YYYY-MM-DD format.")
                    exp_date = None

                if status not in valid_statuses:
                    errors.append(f"Row {line_no}: Invalid status '{status}'. Allowed: {', '.join(sorted(valid_statuses))}.")

                if exp_date and status in ('AVAILABLE', 'RESERVED') and exp_date < today:
                    errors.append(f"Row {line_no}: Past expiry date ({exp_date}) not allowed for {status} bag.")

                if not errors:
                    validated_rows.append({
                        'bag_number': bag_no,
                        'blood_type': b_type,
                        'volume_ml': vol,
                        'expiry_date': exp_date,
                        'status': status,
                        'owner': staff_org,
                    })

            if not errors and validated_rows:
                with transaction.atomic():
                    objs = [BloodInventory(**row) for row in validated_rows]
                    BloodInventory.objects.bulk_create(objs)
                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='CSV_IMPORTED',
                        details={'type': 'bags', 'count': len(objs)}
                    )
                messages.success(request, f"Successfully imported {len(validated_rows)} blood inventory bags.")
                return redirect('dashboard')
            elif not validated_rows and not errors:
                errors.append("The uploaded CSV file contains no data rows.")
    else:
        form = CSVImportForm()

    return render(request, 'blood_ledger/csv_import.html', {
        'form': form,
        'import_type': 'bags',
        'errors': errors,
    })


@login_required
def csv_import_donors_view(request):
    """
    Section 4E: CSV import of donors (blood bank staff only).
    Columns: donor_reference, full_name, blood_type, contact_number, district, latitude, longitude, last_donated_date
    IDs hashed with HMAC-SHA256 via RegisteredDonor.hash_government_id().
    """
    staff_org = _get_staff_org(request.user)
    if not (request.user.is_superuser or (staff_org and staff_org.org_type == 'BLOOD_BANK')):
        raise PermissionDenied("Only blood bank staff can import registered donors.")

    errors = []
    if request.method == 'POST':
        form = CSVImportForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = form.cleaned_data['csv_file']
            try:
                decoded_file = csv_file.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                errors.append("File encoding error: CSV must be encoded in UTF-8.")
                return render(request, 'blood_ledger/csv_import.html', {'form': form, 'import_type': 'donors', 'errors': errors})

            reader = csv.DictReader(io.StringIO(decoded_file))
            expected_fields = {
                'donor_reference', 'full_name', 'blood_type', 'contact_number',
                'district', 'latitude', 'longitude', 'last_donated_date'
            }
            if not expected_fields.issubset(set(f.strip() for f in (reader.fieldnames or []))):
                errors.append(f"Header error: CSV must contain columns: {', '.join(sorted(expected_fields))}")
                return render(request, 'blood_ledger/csv_import.html', {'form': form, 'import_type': 'donors', 'errors': errors})

            valid_blood_types = {b[0] for b in BLOOD_TYPE_CHOICES}
            valid_districts = {d[0] for d in KERALA_DISTRICTS}

            validated_rows = []
            seen_hashes = set()

            for line_no, row in enumerate(reader, start=2):
                donor_ref = row.get('donor_reference', '').strip()
                name = row.get('full_name', '').strip()
                b_type = row.get('blood_type', '').strip()
                phone = row.get('contact_number', '').strip()
                district = row.get('district', '').strip()
                lat_str = row.get('latitude', '').strip()
                lon_str = row.get('longitude', '').strip()
                last_don_str = row.get('last_donated_date', '').strip()

                if not donor_ref or not name or not b_type or not phone or not district or not lat_str or not lon_str:
                    errors.append(f"Row {line_no}: Missing required values.")
                    continue

                hashed_id = RegisteredDonor.hash_government_id(donor_ref)
                if hashed_id in seen_hashes or RegisteredDonor.objects.filter(government_id_hash=hashed_id).exists():
                    errors.append(f"Row {line_no}: Duplicate donor reference '{donor_ref}'.")
                    continue
                seen_hashes.add(hashed_id)

                if b_type not in valid_blood_types:
                    errors.append(f"Row {line_no}: Invalid blood type '{b_type}'.")

                if district not in valid_districts:
                    errors.append(f"Row {line_no}: Invalid Kerala district '{district}'.")

                try:
                    lat = float(lat_str)
                    if not (-90.0 <= lat <= 90.0):
                        errors.append(f"Row {line_no}: Latitude '{lat_str}' out of range (-90 to 90).")
                except ValueError:
                    errors.append(f"Row {line_no}: Latitude '{lat_str}' is not a valid number.")
                    lat = None

                try:
                    lon = float(lon_str)
                    if not (-180.0 <= lon <= 180.0):
                        errors.append(f"Row {line_no}: Longitude '{lon_str}' out of range (-180 to 180).")
                except ValueError:
                    errors.append(f"Row {line_no}: Longitude '{lon_str}' is not a valid number.")
                    lon = None

                last_don_date = None
                if last_don_str:
                    try:
                        last_don_date = datetime.strptime(last_don_str, '%Y-%m-%d').date()
                    except ValueError:
                        errors.append(f"Row {line_no}: Last donated date '{last_don_str}' must be YYYY-MM-DD or empty.")

                if not errors:
                    validated_rows.append({
                        'full_name': name,
                        'government_id_hash': hashed_id,
                        'blood_type': b_type,
                        'contact_number': phone,
                        'district': district,
                        'latitude': lat,
                        'longitude': lon,
                        'last_donated_date': last_don_date,
                        'is_available_for_emergency': True,
                    })

            if not errors and validated_rows:
                with transaction.atomic():
                    objs = [RegisteredDonor(**row) for row in validated_rows]
                    RegisteredDonor.objects.bulk_create(objs)
                    record_audit_log(
                        actor=request.user,
                        organisation=staff_org,
                        action='CSV_IMPORTED',
                        details={'type': 'donors', 'count': len(objs)}
                    )
                messages.success(request, f"Successfully imported {len(validated_rows)} registered donors.")
                return redirect('dashboard')
            elif not validated_rows and not errors:
                errors.append("The uploaded CSV file contains no data rows.")
    else:
        form = CSVImportForm()

    return render(request, 'blood_ledger/csv_import.html', {
        'form': form,
        'import_type': 'donors',
        'errors': errors,
    })


@login_required
def download_csv_template_view(request, template_type):
    """
    Provides downloadable CSV templates for staff data import.
    """
    response = HttpResponse(content_type='text/csv')
    if template_type == 'bags':
        response['Content-Disposition'] = 'attachment; filename="blood_bags_template.csv"'
        writer = csv.writer(response)
        writer.writerow(['bag_number', 'blood_type', 'volume_ml', 'expiry_date', 'status'])
        writer.writerow(['BAG-KL-9001', 'O+', '450', (timezone.now().date() + timedelta(days=28)).strftime('%Y-%m-%d'), 'QUARANTINED'])
        writer.writerow(['BAG-KL-9002', 'A-', '450', (timezone.now().date() + timedelta(days=20)).strftime('%Y-%m-%d'), 'QUARANTINED'])
    elif template_type == 'donors':
        response['Content-Disposition'] = 'attachment; filename="donors_template.csv"'
        writer = csv.writer(response)
        writer.writerow(['donor_reference', 'full_name', 'blood_type', 'contact_number', 'district', 'latitude', 'longitude', 'last_donated_date'])
        writer.writerow(['DON-REF-101', 'Anand Kumar', 'O+', '+91 98470 11223', 'Ernakulam', '9.9816', '76.2999', '2026-06-15'])
        writer.writerow(['DON-REF-102', 'Lekshmi Nair', 'B-', '+91 94471 22334', 'Thrissur', '10.5276', '76.2144', ''])
    else:
        raise Http404("Unknown template type.")
    return response


@login_required
def audit_log_view(request):
    """
    Organisation-scoped audit log with action and date filters, plus pagination.
    """
    staff_org = _get_staff_org(request.user)
    is_admin = request.user.is_superuser

    if not is_admin and not staff_org:
        raise PermissionDenied("User does not have an organisation assigned.")

    logs_qs = AuditLog.objects.select_related('actor', 'organisation', 'emergency_request').order_by('-created_at')
    if not is_admin:
        logs_qs = logs_qs.filter(organisation=staff_org)

    # Filtering by action
    action_filter = request.GET.get('action', '').strip()
    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)

    # Filtering by date
    date_filter = request.GET.get('date', '').strip()
    if date_filter:
        try:
            target_date = datetime.strptime(date_filter, '%Y-%m-%d').date()
            logs_qs = logs_qs.filter(created_at__date=target_date)
        except ValueError:
            pass

    paginator = Paginator(logs_qs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'action_choices': AUDIT_ACTION_CHOICES,
        'selected_action': action_filter,
        'selected_date': date_filter,
        'staff_org': staff_org,
        'is_admin': is_admin,
    }
    return render(request, 'blood_ledger/audit_log.html', context)


def login_view(request):
    """Staff authentication view."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
            next_url = request.GET.get('next') or request.POST.get('next') or 'dashboard'
            return redirect(next_url)
        else:
            messages.error(request, "Invalid username or password.")
    else:
        form = AuthenticationForm()

    return render(request, 'blood_ledger/login.html', {'form': form})


def logout_view(request):
    """Staff logout view."""
    logout(request)
    messages.info(request, "You have been logged out.")
    return redirect('public_search')

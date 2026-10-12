from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from blood_ledger.models import AuditLog, BloodInventory, EmergencyRequest
from blood_ledger.services import record_audit_log


class Command(BaseCommand):
    help = "Expires past-due bags and releases lapsed bag reservations back to stock."

    def handle(self, *args, **options):
        today = timezone.now().date()
        now = timezone.now()

        expired_count = 0
        lapsed_count = 0

        with transaction.atomic():
            # 1. Mark AVAILABLE or QUARANTINED bags past expiry date as EXPIRED
            overdue_bags = BloodInventory.objects.filter(
                status__in=['AVAILABLE', 'QUARANTINED'],
                expiry_date__lt=today
            )
            for bag in overdue_bags:
                old_status = bag.status
                bag.status = 'EXPIRED'
                bag.save(update_fields=['status', 'updated_at'])
                record_audit_log(
                    actor=None,
                    organisation=bag.owner,
                    action='BAG_EXPIRED',
                    bag_number=bag.bag_number,
                    details={
                        'previous_status': old_status,
                        'expiry_date': str(bag.expiry_date),
                    }
                )
                expired_count += 1

            # 2. Release RESERVED bags whose reserved_until has passed
            lapsed_bags = BloodInventory.objects.filter(
                status='RESERVED',
                reserved_until__lt=now
            )
            for bag in lapsed_bags:
                # If the bag has expired in the interim, expire it; otherwise return to AVAILABLE
                if bag.expiry_date < today:
                    bag.status = 'EXPIRED'
                else:
                    bag.status = 'AVAILABLE'
                bag.reserved_until = None
                bag.save(update_fields=['status', 'reserved_until', 'updated_at'])

                # Find associated emergency request
                linked_requests = EmergencyRequest.objects.filter(allocated_bag=bag, status='RESERVED')
                for req in linked_requests:
                    req.allocated_bag = None
                    req.fulfilling_organisation = None
                    # Return to blood bank queue or hospital pending as PENDING
                    req.status = 'PENDING'
                    req.save(update_fields=['allocated_bag', 'fulfilling_organisation', 'status'])

                    record_audit_log(
                        actor=None,
                        organisation=bag.owner,
                        action='RESERVATION_LAPSED',
                        emergency_request=req,
                        bag_number=bag.bag_number,
                        details={
                            'request_id': req.id,
                            'reverted_status': req.status,
                            'bag_new_status': bag.status,
                        }
                    )
                lapsed_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"expire_bags summary: {expired_count} overdue bags marked EXPIRED; "
                f"{lapsed_count} lapsed reservations released."
            )
        )

import hashlib
from datetime import timedelta
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from blood_ledger.models import (
    Organisation,
    StaffProfile,
    BloodInventory,
    RegisteredDonor,
    EmergencyRequest,
    AuditLog,
)
from blood_ledger.services import record_audit_log


class Command(BaseCommand):
    help = "Seeds clearly fictional organisations, inventory units, donors, and test emergency workflows."

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Wipes existing records before seeding new demo data.'
        )

    def handle(self, *args, **options):
        reset = options.get('reset', False)

        has_data = (
            Organisation.objects.exists()
            or BloodInventory.objects.exists()
            or RegisteredDonor.objects.exists()
            or EmergencyRequest.objects.exists()
        )

        if has_data and not reset:
            raise CommandError(
                "Database is not empty. Refusing to run without --reset. "
                "Run 'python manage.py seed_demo --reset' to wipe and reseed."
            )

        self.stdout.write("Seeding RaktaGrid demo environment...")

        with transaction.atomic():
            if reset:
                AuditLog.objects.all().delete()
                EmergencyRequest.objects.all().delete()
                BloodInventory.objects.all().delete()
                StaffProfile.objects.all().delete()
                RegisteredDonor.objects.all().delete()
                Organisation.objects.all().delete()
                User.objects.filter(is_superuser=False).delete()
                User.objects.filter(username='admin').delete()
                self.stdout.write("Existing database records cleared.")

            # 1. Create Superuser (Platform Admin)
            admin_user = User.objects.create_superuser(
                username='admin',
                email='admin@demo-raktagrid.local',
                password='admin123',
                first_name='Civic',
                last_name='Admin'
            )

            # 2. Create 2 Blood Banks and 3 Hospitals across 3 Kerala districts
            bb_ekm = Organisation.objects.create(
                name="Demo Central Blood Bank",
                org_type="BLOOD_BANK",
                district="Ernakulam",
                address="Near Civil Station, Kakkanad, Kochi, Kerala 682030",
                phone="+91 484 2420001",
                latitude=9.9816,
                longitude=76.2999,
                is_active=True,
                is_registered=True,
            )

            bb_kkd = Organisation.objects.create(
                name="Demo Malabar Zonal Blood Bank",
                org_type="BLOOD_BANK",
                district="Kozhikode",
                address="Beach Road, Vellayil, Kozhikode, Kerala 673032",
                phone="+91 495 2360001",
                latitude=11.2588,
                longitude=75.7804,
                is_active=True,
                is_registered=True,
            )

            hosp_a = Organisation.objects.create(
                name="Demo Metro Hospital A",
                org_type="HOSPITAL",
                district="Ernakulam",
                address="Park Avenue, Ernakulam, Kerala 682011",
                phone="+91 484 2350001",
                latitude=9.9850,
                longitude=76.2880,
                is_active=True,
                is_registered=True,
            )

            hosp_b = Organisation.objects.create(
                name="Demo District Hospital B",
                org_type="HOSPITAL",
                district="Thrissur",
                address="Round East, Thrissur, Kerala 680001",
                phone="+91 487 2330001",
                latitude=10.5276,
                longitude=76.2144,
                is_active=True,
                is_registered=True,
            )

            hosp_c = Organisation.objects.create(
                name="Demo North Trauma Center C",
                org_type="HOSPITAL",
                district="Kozhikode",
                address="Mavoor Road, Kozhikode, Kerala 673004",
                phone="+91 495 2720001",
                latitude=11.2650,
                longitude=75.7900,
                is_active=True,
                is_registered=True,
            )

            # 3. Create Staff User for Each Organisation
            staff_definitions = [
                ("bb_central_staff", "password123", bb_ekm, "Central", "Staff"),
                ("bb_malabar_staff", "password123", bb_kkd, "Malabar", "Staff"),
                ("hosp_a_doc", "password123", hosp_a, "Sarah", "Mathew"),
                ("hosp_b_doc", "password123", hosp_b, "Kiran", "Thomas"),
                ("hosp_c_doc", "password123", hosp_c, "Anjali", "Menon"),
            ]

            users_map = {}
            for username, pwd, org, fn, ln in staff_definitions:
                u = User.objects.create_user(
                    username=username,
                    password=pwd,
                    first_name=fn,
                    last_name=ln,
                    email=f"{username}@demo.org"
                )
                StaffProfile.objects.create(user=u, organisation=org)
                users_map[org.id] = u

            # 4. Seed Registered Donors (~15 donors)
            today = timezone.now().date()
            donor_raw_data = [
                # B- donors (for Fallback demo: 0 stock anywhere, so these donors are the rescue!)
                ("DON-KL-001", "Arjun Varma", "B-", "+91 98471 20001", "Ernakulam", 9.9860, 76.2910, today - timedelta(days=120), True),
                ("DON-KL-002", "Deepa Nair", "B-", "+91 94472 20002", "Ernakulam", 9.9920, 76.3050, None, True), # First-time donor
                ("DON-KL-003", "Karthik Raj", "B-", "+91 97450 20003", "Thrissur", 10.5100, 76.2200, today - timedelta(days=95), True),
                ("DON-KL-004", "Rohan Kurian (In Cooldown)", "B-", "+91 94961 20004", "Ernakulam", 9.9830, 76.2890, today - timedelta(days=40), True), # Cooldown active

                # O- donors
                ("DON-KL-005", "Fathima Beevi", "O-", "+91 98952 20005", "Ernakulam", 9.9800, 76.3000, today - timedelta(days=105), True),
                ("DON-KL-006", "Siddharth Shenoy", "O-", "+91 98460 20006", "Kozhikode", 11.2600, 75.7850, today - timedelta(days=150), True),
                ("DON-KL-007", "Manu Pillai (Unavailable)", "O-", "+91 94463 20007", "Ernakulam", 9.9750, 76.2800, None, False), # Unavailable

                # O+ donors
                ("DON-KL-008", "Neeraj Madhav", "O+", "+91 97441 20008", "Ernakulam", 9.9900, 76.3100, today - timedelta(days=110), True),
                ("DON-KL-009", "Pooja Hegde", "O+", "+91 98470 20009", "Thrissur", 10.5250, 76.2100, today - timedelta(days=30), True), # Cooldown active

                # A+ donors
                ("DON-KL-010", "Gautham Vasudev", "A+", "+91 94475 20010", "Thrissur", 10.5300, 76.2180, today - timedelta(days=100), True),
                ("DON-KL-011", "Divya Unni", "A+", "+91 97455 20011", "Ernakulam", 9.9880, 76.2950, None, True),

                # A- donors
                ("DON-KL-012", "Eileen George", "A-", "+91 98955 20012", "Ernakulam", 9.9820, 76.2920, today - timedelta(days=92), True),

                # B+ donors
                ("DON-KL-013", "Harishankar K", "B+", "+91 94965 20013", "Kozhikode", 11.2700, 75.7950, today - timedelta(days=130), True),

                # AB+ donors
                ("DON-KL-014", "Sneha Babu", "AB+", "+91 98465 20014", "Ernakulam", 9.9950, 76.3020, today - timedelta(days=91), True),

                # AB- donors
                ("DON-KL-015", "Tariq Mansoor", "AB-", "+91 94468 20015", "Kozhikode", 11.2550, 75.7750, None, True),
            ]

            donors = []
            for ref_id, name, b_type, phone, dist, lat, lon, l_date, avail in donor_raw_data:
                donor_obj = RegisteredDonor.objects.create(
                    full_name=name,
                    government_id_hash=RegisteredDonor.hash_government_id(ref_id),
                    blood_type=b_type,
                    contact_number=phone,
                    district=dist,
                    latitude=lat,
                    longitude=lon,
                    last_donated_date=l_date,
                    is_available_for_emergency=avail
                )
                donors.append(donor_obj)

            # 5. Seed Blood Inventory (~30 bags)
            # Setup specific scenarios:
            # - Hosp A has available O+ in own stock
            # - Hosp B has 0 A+, but Central Blood Bank has A+ (blood bank path)
            # - AB- has 0 exact stock anywhere, but compatible A- exists in Central Blood Bank
            # - B- has 0 exact stock and 0 compatible O- stock anywhere (demonstrates donor fallback)
            # - Include 1 expired bag
            # - Include some QUARANTINED bags
            bag_samples = [
                # Hospital A Own Stock (O+ available)
                ("BAG-HOSPA-O-01", "O+", 450, today + timedelta(days=25), "AVAILABLE", hosp_a, None),
                ("BAG-HOSPA-O-02", "O+", 450, today + timedelta(days=18), "AVAILABLE", hosp_a, None),
                ("BAG-HOSPA-O-03", "O+", 450, today + timedelta(days=32), "AVAILABLE", hosp_a, None),
                ("BAG-HOSPA-A-01", "A+", 450, today + timedelta(days=15), "AVAILABLE", hosp_a, None),
                ("BAG-HOSPA-Q-01", "O+", 450, today + timedelta(days=30), "QUARANTINED", hosp_a, None),

                # Central Blood Bank Stock (holds A+, A-, B+, AB+, etc.)
                ("BAG-CBB-A-01", "A+", 450, today + timedelta(days=22), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-A-02", "A+", 450, today + timedelta(days=12), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-A-03", "A+", 450, today + timedelta(days=35), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-A-NEG-01", "A-", 450, today + timedelta(days=28), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-A-NEG-02", "A-", 450, today + timedelta(days=14), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-B-01", "B+", 450, today + timedelta(days=20), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-B-02", "B+", 450, today + timedelta(days=26), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-AB-01", "AB+", 450, today + timedelta(days=19), "AVAILABLE", bb_ekm, None),
                ("BAG-CBB-Q-01", "B+", 450, today + timedelta(days=30), "QUARANTINED", bb_ekm, donors[12]),
                ("BAG-CBB-Q-02", "A+", 450, today + timedelta(days=34), "QUARANTINED", bb_ekm, donors[10]),

                # Malabar Blood Bank Stock
                ("BAG-MBB-O-01", "O+", 450, today + timedelta(days=29), "AVAILABLE", bb_kkd, None),
                ("BAG-MBB-A-01", "A+", 450, today + timedelta(days=17), "AVAILABLE", bb_kkd, None),
                ("BAG-MBB-B-01", "B+", 450, today + timedelta(days=21), "AVAILABLE", bb_kkd, None),
                ("BAG-MBB-AB-01", "AB+", 450, today + timedelta(days=25), "AVAILABLE", bb_kkd, None),
                ("BAG-MBB-Q-01", "O+", 450, today + timedelta(days=35), "QUARANTINED", bb_kkd, None),

                # Hospital B Stock (Has B+, but NO A+ and NO B-)
                ("BAG-HOSPB-B-01", "B+", 450, today + timedelta(days=24), "AVAILABLE", hosp_b, None),
                ("BAG-HOSPB-B-02", "B+", 450, today + timedelta(days=30), "AVAILABLE", hosp_b, None),
                ("BAG-HOSPB-Q-01", "B+", 450, today + timedelta(days=31), "QUARANTINED", hosp_b, None),

                # Hospital C Stock
                ("BAG-HOSPC-O-01", "O+", 450, today + timedelta(days=16), "AVAILABLE", hosp_c, None),
                ("BAG-HOSPC-A-01", "A+", 450, today + timedelta(days=23), "AVAILABLE", hosp_c, None),

                # Expired Bag (1 expired unit in storage)
                ("BAG-EXP-001", "O+", 450, today - timedelta(days=4), "EXPIRED", bb_ekm, None),
                ("BAG-EXP-002", "A+", 450, today - timedelta(days=8), "EXPIRED", hosp_a, None),

                # Dispatched Bags
                ("BAG-DISP-001", "O+", 450, today + timedelta(days=15), "DISPATCHED", hosp_a, None),
                ("BAG-DISP-002", "A+", 450, today + timedelta(days=18), "DISPATCHED", bb_ekm, None),
            ]

            for bag_no, b_type, vol, exp, stat, owner_org, src_donor in bag_samples:
                BloodInventory.objects.create(
                    bag_number=bag_no,
                    blood_type=b_type,
                    volume_ml=vol,
                    expiry_date=exp,
                    status=stat,
                    owner=owner_org,
                    source_donor=src_donor
                )

            # 6. Seed 3 Demo Requests:
            # (a) Request for O+ at Hospital A -> Requested to Demo Central Blood Bank
            req_a = EmergencyRequest.objects.create(
                requesting_hospital=hosp_a,
                requested_by=users_map[hosp_a.id],
                required_blood_type="O+",
                patient_reference="ICU-BED-04",
                blood_bank=bb_ekm,
                status="PENDING"
            )
            record_audit_log(
                actor=users_map[hosp_a.id],
                organisation=hosp_a,
                action='REQUEST_CREATED',
                emergency_request=req_a,
                details={'scenario': 'Requisition requested to Demo Central Blood Bank'}
            )
            record_audit_log(
                actor=users_map[hosp_a.id],
                organisation=hosp_a,
                action='BANK_REQUESTED',
                emergency_request=req_a,
                details={
                    'blood_bank_id': bb_ekm.id,
                    'blood_bank_name': bb_ekm.name,
                    'required_blood_type': 'O+',
                }
            )

            # (b) Request for A+ at Hospital B -> Routed to Demo Central Blood Bank
            req_b = EmergencyRequest.objects.create(
                requesting_hospital=hosp_b,
                requested_by=users_map[hosp_b.id],
                required_blood_type="A+",
                patient_reference="TRAUMA-BAY-12",
                blood_bank=bb_ekm,
                status="PENDING"
            )
            record_audit_log(
                actor=users_map[hosp_b.id],
                organisation=hosp_b,
                action='REQUEST_CREATED',
                emergency_request=req_b,
                details={'scenario': 'Demonstrates Step 3 & 4: Sourcing from nearby regional blood bank'}
            )
            record_audit_log(
                actor=users_map[hosp_b.id],
                organisation=hosp_b,
                action='BANK_REQUESTED',
                emergency_request=req_b,
                details={
                    'blood_bank_id': bb_ekm.id,
                    'blood_bank_name': bb_ekm.name,
                    'required_blood_type': 'A+',
                }
            )

            # (c) Request for B- at Hospital A -> Requested to Demo Central Blood Bank
            req_c = EmergencyRequest.objects.create(
                requesting_hospital=hosp_a,
                requested_by=users_map[hosp_a.id],
                required_blood_type="B-",
                patient_reference="OT-CASE-889",
                blood_bank=bb_ekm,
                status="PENDING"
            )
            record_audit_log(
                actor=users_map[hosp_a.id],
                organisation=hosp_a,
                action='REQUEST_CREATED',
                emergency_request=req_c,
                details={'scenario': 'Requisition requested to Demo Central Blood Bank'}
            )
            record_audit_log(
                actor=users_map[hosp_a.id],
                organisation=hosp_a,
                action='BANK_REQUESTED',
                emergency_request=req_c,
                details={
                    'blood_bank_id': bb_ekm.id,
                    'blood_bank_name': bb_ekm.name,
                    'required_blood_type': 'B-',
                }
            )

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 65))
        self.stdout.write(self.style.SUCCESS("RAKTAGRID DEMO SEEDING COMPLETED SUCCESSFULLY"))
        self.stdout.write(self.style.SUCCESS("=" * 65))
        self.stdout.write("\nDEMO USER CREDENTIALS:")
        self.stdout.write("  - Superuser (Platform Admin): admin / admin123")
        self.stdout.write("  - Demo Metro Hospital A (Ernakulam): hosp_a_doc / password123")
        self.stdout.write("  - Demo District Hospital B (Thrissur): hosp_b_doc / password123")
        self.stdout.write("  - Demo North Trauma Center C (Kozhikode): hosp_c_doc / password123")
        self.stdout.write("  - Demo Central Blood Bank (Ernakulam): bb_central_staff / password123")
        self.stdout.write("  - Demo Malabar Zonal Blood Bank (Kozhikode): bb_malabar_staff / password123")
        self.stdout.write("\nSEEDED WORKFLOW SCENARIOS:")
        self.stdout.write(f"  1. Request #{req_a.id:05d} (O+): Hospital A can reserve from OWN stock.")
        self.stdout.write(f"  2. Request #{req_b.id:05d} (A+): Hospital B has no stock; request Central Blood Bank.")
        self.stdout.write(f"  3. Request #{req_c.id:05d} (B-): Zero stock anywhere; triggers Voluntary Donor Fallback.")
        self.stdout.write("=" * 65 + "\n")

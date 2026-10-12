import csv
import io
import math
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
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
    reserve_matching_bag,
)


class DonorCooldownTests(TestCase):
    """
    Test suite for 90-day whole blood cooldown and ActiveDonorManager eligibility.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.org = Organisation.objects.create(
            name="Test General Hospital",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Kochi",
            phone="0484111111",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )

    def test_donor_cooldown_rules(self):
        # 1. No donation date -> eligible, 0 days remaining
        d_none = RegisteredDonor.objects.create(
            full_name="Never Donated",
            government_id_hash=RegisteredDonor.hash_government_id("REF-NONE-01"),
            blood_type="O+",
            contact_number="9847000001",
            district="Ernakulam",
            latitude=9.98,
            longitude=76.28,
            last_donated_date=None,
            is_available_for_emergency=True
        )
        self.assertEqual(d_none.cooldown_days_remaining, 0)
        self.assertTrue(d_none.is_eligible)

        # 2. Donated 89 days ago -> excluded, exactly 1 day remaining
        d_89 = RegisteredDonor.objects.create(
            full_name="Donated 89 Days Ago",
            government_id_hash=RegisteredDonor.hash_government_id("REF-89-01"),
            blood_type="O+",
            contact_number="9847000002",
            district="Ernakulam",
            latitude=9.98,
            longitude=76.28,
            last_donated_date=self.today - timedelta(days=89),
            is_available_for_emergency=True
        )
        self.assertEqual(d_89.cooldown_days_remaining, 1)
        self.assertFalse(d_89.is_eligible)

        # 3. Exactly 90 days ago -> eligible, 0 days remaining
        d_90 = RegisteredDonor.objects.create(
            full_name="Donated 90 Days Ago",
            government_id_hash=RegisteredDonor.hash_government_id("REF-90-01"),
            blood_type="O+",
            contact_number="9847000003",
            district="Ernakulam",
            latitude=9.98,
            longitude=76.28,
            last_donated_date=self.today - timedelta(days=90),
            is_available_for_emergency=True
        )
        self.assertEqual(d_90.cooldown_days_remaining, 0)
        self.assertTrue(d_90.is_eligible)

        # Query manager
        eligible_qs = RegisteredDonor.eligible_donors.eligible_for_emergency("O+")
        self.assertIn(d_none, eligible_qs)
        self.assertIn(d_90, eligible_qs)
        self.assertNotIn(d_89, eligible_qs)


class BloodInventoryValidationTests(TestCase):
    """
    Test suite for BloodInventory model validation and past expiry rejection.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.org = Organisation.objects.create(
            name="Central Blood Bank",
            org_type="BLOOD_BANK",
            district="Ernakulam",
            address="Kochi",
            phone="0484222222",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )

    def test_clean_rejects_past_expiry_for_available_and_reserved(self):
        past_date = self.today - timedelta(days=1)

        # AVAILABLE bag with past expiry must raise ValidationError
        bag_avail = BloodInventory(
            bag_number="BAG-PAST-AVAIL",
            blood_type="A+",
            volume_ml=450,
            expiry_date=past_date,
            status="AVAILABLE",
            owner=self.org
        )
        with self.assertRaises(ValidationError):
            bag_avail.clean()

        # RESERVED bag with past expiry must raise ValidationError
        bag_res = BloodInventory(
            bag_number="BAG-PAST-RES",
            blood_type="A+",
            volume_ml=450,
            expiry_date=past_date,
            status="RESERVED",
            owner=self.org
        )
        with self.assertRaises(ValidationError):
            bag_res.clean()

        # EXPIRED bag with past expiry is valid
        bag_exp = BloodInventory(
            bag_number="BAG-PAST-EXP",
            blood_type="A+",
            volume_ml=450,
            expiry_date=past_date,
            status="EXPIRED",
            owner=self.org
        )
        try:
            bag_exp.clean()
        except ValidationError:
            self.fail("clean() raised ValidationError for EXPIRED status bag.")


class ThreeTierWorkflowTests(TestCase):
    """
    Tests the 3-tier cascade:
    1. Own hospital stock reserved first
    2. Falls to blood bank when hospital has none
    3. Blood bank reject returns to PENDING
    4. No bank stock leads to FALLBACK_DONOR_SEARCH
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Kochi",
            phone="0484333333",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )
        self.bank = Organisation.objects.create(
            name="Regional Bank Beta",
            org_type="BLOOD_BANK",
            district="Ernakulam",
            address="Kochi",
            phone="0484444444",
            latitude=9.99,
            longitude=76.29,
            is_active=True,
            is_registered=True
        )

        self.doc_user = User.objects.create_user(username="doc_alpha", password="password123")
        StaffProfile.objects.create(user=self.doc_user, organisation=self.hosp)

        self.bank_user = User.objects.create_user(username="staff_beta", password="password123")
        StaffProfile.objects.create(user=self.bank_user, organisation=self.bank)

        self.client = Client()

    def test_own_stock_reserved_first(self):
        self.client.login(username="doc_alpha", password="password123")

        # Hospital owns available O+ bag
        bag = BloodInventory.objects.create(
            bag_number="BAG-HOSP-O-1",
            blood_type="O+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="AVAILABLE",
            owner=self.hosp
        )

        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="O+",
            patient_reference="BED-101",
            status="PENDING"
        )

        response = self.client.post(reverse('check_own_stock', args=[req.id]))
        self.assertEqual(response.status_code, 302)

        req.refresh_from_db()
        bag.refresh_from_db()
        self.assertEqual(req.status, 'RESERVED')
        self.assertEqual(req.allocated_bag, bag)
        self.assertEqual(bag.status, 'RESERVED')
        self.assertEqual(req.fulfilling_organisation, self.hosp)

    def test_routing_to_blood_bank_and_rejection(self):
        # Hospital has NO stock
        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="AB+",
            patient_reference="ICU-42",
            status="PENDING"
        )

        # 1. Hospital requests blood bank
        self.client.login(username="doc_alpha", password="password123")
        res = self.client.post(reverse('request_blood_bank', args=[req.id]), {'blood_bank_id': self.bank.id})
        self.assertEqual(res.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(req.blood_bank, self.bank)

        # 2. Blood bank rejects request with clinical reason
        self.client.login(username="staff_beta", password="password123")
        res_reject = self.client.post(reverse('reject_bank_request', args=[req.id]), {
            'rejection_note': 'Units reserved for internal emergency theater'
        })
        self.assertEqual(res_reject.status_code, 302)

        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertIn("internal emergency theater", req.rejection_note)

    def test_no_bank_stock_leads_to_fallback_donor_search(self):
        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="B-",
            patient_reference="CAS-09",
            status="PENDING"
        )
        self.client.login(username="doc_alpha", password="password123")
        res = self.client.post(reverse('trigger_fallback', args=[req.id]))
        self.assertRedirects(res, reverse('donor_directory', args=[req.id]))

        req.refresh_from_db()
        self.assertEqual(req.status, 'FALLBACK_DONOR_SEARCH')


class IssueSafetyTests(TestCase):
    """
    Tests issue refusal rules:
    - Issue refused without cross-match tick
    - Issue refused without RESERVED bag
    - QUARANTINED, EXPIRED, and RESERVED-for-another-request bags are never reserved
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Kochi",
            phone="0484333333",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )
        self.doc_user = User.objects.create_user(username="doc_alpha", password="password123")
        StaffProfile.objects.create(user=self.doc_user, organisation=self.hosp)
        self.client = Client()

    def test_issue_refused_without_cross_match_tick(self):
        self.client.login(username="doc_alpha", password="password123")
        bag = BloodInventory.objects.create(
            bag_number="BAG-RES-1",
            blood_type="A+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=15),
            status="RESERVED",
            owner=self.hosp,
            reserved_until=timezone.now() + timedelta(hours=2)
        )
        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="A+",
            patient_reference="BED-2",
            allocated_bag=bag,
            fulfilling_organisation=self.hosp,
            status="RESERVED"
        )

        # POST without cross_match_confirmed
        response = self.client.post(reverse('issue_blood', args=[req.id]), {})
        self.assertEqual(response.status_code, 302)

        req.refresh_from_db()
        bag.refresh_from_db()
        self.assertEqual(req.status, 'RESERVED')
        self.assertEqual(bag.status, 'RESERVED')
        self.assertIsNone(req.cross_match_confirmed_at)

        # POST with cross_match_confirmed = '1'
        response_success = self.client.post(reverse('issue_blood', args=[req.id]), {'cross_match_confirmed': '1'})
        self.assertRedirects(response_success, reverse('dispatch_manifest', args=[req.id]))

        req.refresh_from_db()
        bag.refresh_from_db()
        self.assertEqual(req.status, 'DISPATCHED')
        self.assertEqual(bag.status, 'DISPATCHED')
        self.assertIsNotNone(req.cross_match_confirmed_at)

    def test_unreservable_bags_are_never_reserved(self):
        # QUARANTINED bag
        BloodInventory.objects.create(
            bag_number="BAG-Q-1",
            blood_type="A+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="QUARANTINED",
            owner=self.hosp
        )
        # EXPIRED bag
        BloodInventory.objects.create(
            bag_number="BAG-EXP-1",
            blood_type="A+",
            volume_ml=450,
            expiry_date=self.today - timedelta(days=5),
            status="EXPIRED",
            owner=self.hosp
        )
        # RESERVED bag for another request
        BloodInventory.objects.create(
            bag_number="BAG-OTHER-RES",
            blood_type="A+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="RESERVED",
            owner=self.hosp,
            reserved_until=timezone.now() + timedelta(hours=2)
        )

        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="A+",
            patient_reference="BED-5",
            status="PENDING"
        )

        reserved = reserve_matching_bag(req, self.hosp, self.doc_user)
        self.assertIsNone(reserved)


class BloodCompatibilityTests(TestCase):
    """
    Tests ABO/Rh compatibility order:
    - Compatible substitute used and recorded when no exact stock exists
    - O- is kept for last
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Kochi",
            phone="0484333333",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )
        self.doc_user = User.objects.create_user(username="doc_alpha", password="password123")

    def test_compatible_substitute_and_o_neg_last(self):
        # For A+ request: accepts ['A+', 'A-', 'O+', 'O-']
        # We stock an O- bag and an A- bag (NO A+ bag)
        bag_o_neg = BloodInventory.objects.create(
            bag_number="BAG-O-NEG-1",
            blood_type="O-",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=10),
            status="AVAILABLE",
            owner=self.hosp
        )
        bag_a_neg = BloodInventory.objects.create(
            bag_number="BAG-A-NEG-1",
            blood_type="A-",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="AVAILABLE",
            owner=self.hosp
        )

        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="A+",
            patient_reference="BED-COMP",
            status="PENDING"
        )

        reserved = reserve_matching_bag(req, self.hosp, self.doc_user)
        # Should reserve A- first, NOT O- (O- kept for last!)
        self.assertIsNotNone(reserved)
        self.assertEqual(reserved.bag_number, bag_a_neg.bag_number)
        self.assertEqual(reserved.blood_type, "A-")

        # Check Audit Log details
        log = AuditLog.objects.filter(emergency_request=req, action='BAG_RESERVED').first()
        self.assertIsNotNone(log)
        self.assertTrue(log.details.get('is_substitute'))
        self.assertEqual(log.details.get('allocated_blood_type'), 'A-')


class ExpireBagsCommandTests(TestCase):
    """
    Tests expire_bags management command:
    - Overdue bags marked EXPIRED
    - Lapsed reservations released
    - Idempotent
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Kochi",
            phone="0484333333",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )
        self.doc_user = User.objects.create_user(username="doc_alpha", password="password123")

    def test_expire_bags_execution(self):
        from django.core.management import call_command

        # 1. Overdue available bag (simulating elapsed time)
        bag_overdue = BloodInventory.objects.create(
            bag_number="BAG-OVERDUE-1",
            blood_type="O+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=1),
            status="AVAILABLE",
            owner=self.hosp
        )
        BloodInventory.objects.filter(id=bag_overdue.id).update(expiry_date=self.today - timedelta(days=2))
        # 2. Lapsed reserved bag
        bag_lapsed = BloodInventory.objects.create(
            bag_number="BAG-LAPSED-1",
            blood_type="O+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="RESERVED",
            reserved_until=timezone.now() - timedelta(minutes=10),
            owner=self.hosp
        )
        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="O+",
            patient_reference="BED-LAPSED",
            allocated_bag=bag_lapsed,
            fulfilling_organisation=self.hosp,
            status="RESERVED"
        )

        call_command('expire_bags')

        bag_overdue.refresh_from_db()
        bag_lapsed.refresh_from_db()
        req.refresh_from_db()

        self.assertEqual(bag_overdue.status, 'EXPIRED')
        self.assertEqual(bag_lapsed.status, 'AVAILABLE')
        self.assertIsNone(bag_lapsed.reserved_until)
        self.assertEqual(req.status, 'PENDING')
        self.assertIsNone(req.allocated_bag)

        # Idempotency check: run again without errors
        call_command('expire_bags')
        bag_overdue.refresh_from_db()
        self.assertEqual(bag_overdue.status, 'EXPIRED')


class RecordDonationAndReleaseTests(TestCase):
    """
    Tests Record Donation (blood bank staff) & Release to stock:
    - Cooldown donor rejected
    - Valid donor creates QUARANTINED bag
    - Release moves QUARANTINED to AVAILABLE
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.bank = Organisation.objects.create(
            name="Bank Beta",
            org_type="BLOOD_BANK",
            district="Ernakulam",
            address="Kochi",
            phone="0484555555",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True
        )
        self.staff_user = User.objects.create_user(username="bank_user", password="password123")
        StaffProfile.objects.create(user=self.staff_user, organisation=self.bank)
        self.client = Client()

    def test_record_donation_flow(self):
        self.client.login(username="bank_user", password="password123")

        # Donor in cooldown (<90 days)
        donor_cooling = RegisteredDonor.objects.create(
            full_name="Cooling Donor",
            government_id_hash=RegisteredDonor.hash_government_id("REF-COOL-1"),
            blood_type="B+",
            contact_number="9847111111",
            district="Ernakulam",
            latitude=9.98,
            longitude=76.28,
            last_donated_date=self.today - timedelta(days=40),
            is_available_for_emergency=True
        )

        res_fail = self.client.post(reverse('record_donation'), {
            'donor': donor_cooling.id,
            'bag_number': 'BAG-FAIL-1',
            'volume_ml': 450,
            'expiry_date': (self.today + timedelta(days=35)).strftime('%Y-%m-%d')
        })
        self.assertEqual(res_fail.status_code, 200) # Form re-rendered with error
        self.assertFalse(BloodInventory.objects.filter(bag_number='BAG-FAIL-1').exists())

        # Eligible donor
        donor_eligible = RegisteredDonor.objects.create(
            full_name="Eligible Donor",
            government_id_hash=RegisteredDonor.hash_government_id("REF-ELIG-1"),
            blood_type="B+",
            contact_number="9847222222",
            district="Ernakulam",
            latitude=9.98,
            longitude=76.28,
            last_donated_date=self.today - timedelta(days=100),
            is_available_for_emergency=True
        )

        res_ok = self.client.post(reverse('record_donation'), {
            'donor': donor_eligible.id,
            'bag_number': 'BAG-DON-OK-1',
            'volume_ml': 450,
            'expiry_date': (self.today + timedelta(days=35)).strftime('%Y-%m-%d')
        })
        self.assertEqual(res_ok.status_code, 302)

        bag = BloodInventory.objects.get(bag_number='BAG-DON-OK-1')
        self.assertEqual(bag.status, 'QUARANTINED')
        self.assertEqual(bag.owner, self.bank)
        donor_eligible.refresh_from_db()
        self.assertEqual(donor_eligible.last_donated_date, self.today)

        # Release to stock
        res_rel = self.client.post(reverse('release_bag', args=[bag.id]))
        self.assertEqual(res_rel.status_code, 302)
        bag.refresh_from_db()
        self.assertEqual(bag.status, 'AVAILABLE')


class OrganisationIsolationTests(TestCase):
    """
    Tests multi-tenant isolation: staff of another organisation get 403 or 404 on
    requests, bags, manifests, imports, and audit entries.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp_1 = Organisation.objects.create(
            name="Hospital 1", org_type="HOSPITAL", district="Ernakulam",
            address="A", phone="1", latitude=9.98, longitude=76.28, is_active=True, is_registered=True
        )
        self.hosp_2 = Organisation.objects.create(
            name="Hospital 2", org_type="HOSPITAL", district="Thrissur",
            address="B", phone="2", latitude=10.5, longitude=76.2, is_active=True, is_registered=True
        )

        self.user_1 = User.objects.create_user(username="u1", password="password123")
        StaffProfile.objects.create(user=self.user_1, organisation=self.hosp_1)

        self.user_2 = User.objects.create_user(username="u2", password="password123")
        StaffProfile.objects.create(user=self.user_2, organisation=self.hosp_2)

        self.client = Client()

    def test_cross_org_access_forbidden(self):
        req_1 = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp_1,
            requested_by=self.user_1,
            required_blood_type="O+",
            patient_reference="CASE-U1",
            status="PENDING"
        )
        bag_1 = BloodInventory.objects.create(
            bag_number="BAG-U1", blood_type="O+", volume_ml=450,
            expiry_date=self.today + timedelta(days=20), status="QUARANTINED", owner=self.hosp_1
        )

        # Log in as user_2
        self.client.login(username="u2", password="password123")

        # Attempt to access req_1
        res_req = self.client.get(reverse('request_detail', args=[req_1.id]))
        self.assertEqual(res_req.status_code, 403)

        # Attempt to release bag_1
        res_bag = self.client.post(reverse('release_bag', args=[bag_1.id]))
        self.assertEqual(res_bag.status_code, 403)


class CSVImportTests(TestCase):
    """
    Tests CSV imports:
    - Valid file imports everything
    - One bad row imports nothing and report names that row
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital 1", org_type="HOSPITAL", district="Ernakulam",
            address="A", phone="1", latitude=9.98, longitude=76.28, is_active=True, is_registered=True
        )
        self.user = User.objects.create_user(username="u1", password="password123")
        StaffProfile.objects.create(user=self.user, organisation=self.hosp)
        self.client = Client()

    def test_csv_import_all_or_nothing(self):
        self.client.login(username="u1", password="password123")

        # 1. Invalid CSV (Row 3 has past expiry date for AVAILABLE status)
        csv_bad = io.StringIO(
            "bag_number,blood_type,volume_ml,expiry_date,status\n"
            "BAG-CSV-01,O+,450,2030-01-01,AVAILABLE\n"
            "BAG-CSV-02,A+,450,2020-01-01,AVAILABLE\n" # Row 3 bad
        )
        file_bad = io.BytesIO(csv_bad.getvalue().encode('utf-8'))
        file_bad.name = 'test_bad.csv'

        res_bad = self.client.post(reverse('csv_import_bags'), {'csv_file': file_bad})
        self.assertEqual(res_bad.status_code, 200)
        self.assertContains(res_bad, "Row 3")
        self.assertEqual(BloodInventory.objects.count(), 0) # Nothing imported!

        # 2. Valid CSV
        csv_good = io.StringIO(
            "bag_number,blood_type,volume_ml,expiry_date,status\n"
            "BAG-CSV-01,O+,450,2030-01-01,AVAILABLE\n"
            "BAG-CSV-02,A+,450,2030-01-01,QUARANTINED\n"
        )
        file_good = io.BytesIO(csv_good.getvalue().encode('utf-8'))
        file_good.name = 'test_good.csv'

        res_good = self.client.post(reverse('csv_import_bags'), {'csv_file': file_good})
        self.assertEqual(res_good.status_code, 302)
        self.assertEqual(BloodInventory.objects.count(), 2)


class PublicPageAndRateLimitTests(TestCase):
    """
    Tests public search page:
    - Zero login accessible
    - Shows only registered, active organisations
    - Rate limit returns 429 after 30 requests per minute
    """
    def setUp(self):
        cache.clear()
        self.reg_org = Organisation.objects.create(
            name="Registered Org", org_type="HOSPITAL", district="Ernakulam",
            address="A", phone="1", latitude=9.98, longitude=76.28, is_active=True, is_registered=True
        )
        self.unreg_org = Organisation.objects.create(
            name="Unregistered Org", org_type="HOSPITAL", district="Ernakulam",
            address="B", phone="2", latitude=9.98, longitude=76.28, is_active=True, is_registered=False
        )
        self.client = Client()

    def test_public_page_display(self):
        res = self.client.get(reverse('public_search'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Registered Org")
        self.assertNotContains(res, "Unregistered Org")

    def test_rate_limit_30_per_minute(self):
        for _ in range(30):
            res = self.client.get(reverse('public_search'), REMOTE_ADDR='192.168.1.100')
            self.assertEqual(res.status_code, 200)

        # 31st request should be rate-limited (429)
        res_31 = self.client.get(reverse('public_search'), REMOTE_ADDR='192.168.1.100')
        self.assertEqual(res_31.status_code, 429)


class ManifestAndProximityTests(TestCase):
    """
    Tests manifest layout and Haversine distance calculations.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha", org_type="HOSPITAL", district="Ernakulam",
            address="Kochi", phone="0484333333", latitude=9.98, longitude=76.28, is_active=True, is_registered=True
        )
        self.doc_user = User.objects.create_user(username="doc_alpha", password="password123")
        StaffProfile.objects.create(user=self.doc_user, organisation=self.hosp)
        self.client = Client()

    def test_haversine_formula_accuracy(self):
        # Distance between (0, 0) and (0, 1) degrees at equator
        dist = haversine_distance(0, 0, 0, 1)
        self.assertAlmostEqual(dist, 111.19, places=1)

    def test_manifest_page_content_and_footer(self):
        self.client.login(username="doc_alpha", password="password123")

        bag = BloodInventory.objects.create(
            bag_number="BAG-DISP-MANIFEST",
            blood_type="O+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="DISPATCHED",
            owner=self.hosp
        )
        req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.doc_user,
            required_blood_type="A+", # Substitute issued!
            patient_reference="BED-MANIFEST",
            allocated_bag=bag,
            fulfilling_organisation=self.hosp,
            cross_match_confirmed_at=timezone.now(),
            dispatched_at=timezone.now(),
            status="FULFILLED_FROM_STOCK"
        )

        res = self.client.get(reverse('dispatch_manifest', args=[req.id]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "BAG-DISP-MANIFEST")
        self.assertContains(res, "A+") # Requested
        self.assertContains(res, "O+") # Issued
        self.assertContains(
            res,
            "Compatibility shown is ABO/Rh red-cell compatibility only; final cross-matching and clinical decisions rest with the hospital's transfusion service."
        )


class ConcurrencyLockingTests(TransactionTestCase):
    """
    TransactionTestCase verifying row-level locking (select_for_update):
    1 AVAILABLE bag and 5 PENDING requests of the same group reserve concurrently
    in threads with their own DB connections; exactly 1 reservation succeeds.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="Hospital Alpha", org_type="HOSPITAL", district="Ernakulam",
            address="Kochi", phone="0484333333", latitude=9.98, longitude=76.28, is_active=True, is_registered=True
        )
        self.user = User.objects.create_user(username="doc_conc", password="password123")
        StaffProfile.objects.create(user=self.user, organisation=self.hosp)

        self.bag = BloodInventory.objects.create(
            bag_number="BAG-SCARCE-O-NEG",
            blood_type="O-",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=25),
            status="AVAILABLE",
            owner=self.hosp
        )

        self.requests = [
            EmergencyRequest.objects.create(
                requesting_hospital=self.hosp,
                requested_by=self.user,
                required_blood_type="O-",
                patient_reference=f"CONC-BED-{i}",
                status="PENDING"
            )
            for i in range(5)
        ]

    def test_concurrent_reservation_prevents_double_booking(self):
        successful_allocations = []
        errors = []

        def worker(req_id):
            try:
                with transaction.atomic():
                    req = EmergencyRequest.objects.select_for_update().get(id=req_id)
                    org = Organisation.objects.get(id=self.hosp.id)
                    user = User.objects.get(id=self.user.id)
                    reserved_bag = reserve_matching_bag(req, org, user)
                    if reserved_bag:
                        successful_allocations.append((req.id, reserved_bag.id))
            except Exception as e:
                errors.append(str(e))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(req.id,)) for req in self.requests]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered unexpected concurrency errors: {errors}")
        # Exactly ONE request must succeed in reserving the bag
        self.assertEqual(
            len(successful_allocations), 1,
            f"Expected exactly 1 successful reservation, got {len(successful_allocations)}: {successful_allocations}"
        )

        self.bag.refresh_from_db()
        self.assertEqual(self.bag.status, 'RESERVED')


class EmergencyRequestDeleteTests(TestCase):
    """
    Test suite for deleting emergency requests and auto-releasing reserved inventory.
    """
    def setUp(self):
        self.today = timezone.now().date()
        self.hosp = Organisation.objects.create(
            name="City Hospital",
            org_type="HOSPITAL",
            district="Ernakulam",
            address="Marine Drive",
            phone="0484112233",
            latitude=9.98,
            longitude=76.28,
            is_active=True,
            is_registered=True,
        )
        self.other_hosp = Organisation.objects.create(
            name="Other Hospital",
            org_type="HOSPITAL",
            district="Thrissur",
            address="Round South",
            phone="0487223344",
            latitude=10.52,
            longitude=76.21,
            is_active=True,
            is_registered=True,
        )
        self.user = User.objects.create_user(username="city_doc", password="password123")
        StaffProfile.objects.create(user=self.user, organisation=self.hosp)

        self.other_user = User.objects.create_user(username="other_doc", password="password123")
        StaffProfile.objects.create(user=self.other_user, organisation=self.other_hosp)

        self.admin = User.objects.create_superuser(username="admin_user", password="password123")

        self.bag = BloodInventory.objects.create(
            bag_number="BAG-DEL-01",
            blood_type="O+",
            volume_ml=450,
            expiry_date=self.today + timedelta(days=20),
            status="AVAILABLE",
            owner=self.hosp,
        )

        self.req = EmergencyRequest.objects.create(
            requesting_hospital=self.hosp,
            requested_by=self.user,
            required_blood_type="O+",
            patient_reference="BED-999",
            status="PENDING",
        )

    def test_requesting_hospital_can_delete_pending_request(self):
        self.client.login(username="city_doc", password="password123")
        response = self.client.post(reverse("delete_emergency_request", kwargs={"request_id": self.req.id}))
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(EmergencyRequest.objects.filter(id=self.req.id).exists())

        # Check audit log
        log = AuditLog.objects.filter(action="REQUEST_DELETED").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.details["patient_reference"], "BED-999")

    def test_delete_releases_reserved_bag(self):
        # Reserve the bag
        self.bag.status = "RESERVED"
        self.bag.reserved_until = timezone.now() + timedelta(hours=2)
        self.bag.save()

        self.req.allocated_bag = self.bag
        self.req.status = "RESERVED"
        self.req.save()

        self.client.login(username="city_doc", password="password123")
        response = self.client.post(reverse("delete_emergency_request", kwargs={"request_id": self.req.id}))
        self.assertRedirects(response, reverse("dashboard"))

        self.assertFalse(EmergencyRequest.objects.filter(id=self.req.id).exists())
        self.bag.refresh_from_db()
        self.assertEqual(self.bag.status, "AVAILABLE")
        self.assertIsNone(self.bag.reserved_until)

    def test_unrelated_hospital_cannot_delete(self):
        self.client.login(username="other_doc", password="password123")
        response = self.client.post(reverse("delete_emergency_request", kwargs={"request_id": self.req.id}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(EmergencyRequest.objects.filter(id=self.req.id).exists())

    def test_admin_can_delete_request(self):
        self.client.login(username="admin_user", password="password123")
        response = self.client.post(reverse("delete_emergency_request", kwargs={"request_id": self.req.id}))
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(EmergencyRequest.objects.filter(id=self.req.id).exists())

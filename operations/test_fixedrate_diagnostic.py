from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from . import price_cards
from .general_price_handoff import import_general_dpot
from .models import FixedRate, OperationalZone, PriceCard, User


class FixedRateDiagnosticTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("diagnostic-admin", role="admin")
        import_general_dpot(actor=cls.admin)
        cls.card = PriceCard.objects.get(scope_key="general")

    def extra(self, origin_name="Madina"):
        price_cards.save_route_price(
            actor=self.admin,
            card_id=self.card.pk,
            origin_id=OperationalZone.objects.get(name=origin_name).pk,
            destination_id=OperationalZone.objects.get(name="Adenta").pk,
            bike_price="55",
            car_price="85",
            bidirectional=False,
            reason="Diagnostic test extra route",
        )

    def report(self):
        output = StringIO()
        call_command("diagnose_general_fixedrate_extras", stdout=output)
        return output.getvalue()

    def test_no_extras_reports_none(self):
        output = self.report()
        self.assertIn("EXTRA ROW COUNT: 0", output)
        self.assertIn("total=193", output)

    def test_two_extras_identity_selection_and_zero_writes(self):
        self.extra()
        before = list(FixedRate.objects.order_by("pk").values())
        with CaptureQueriesContext(connection) as queries:
            output = self.report()
        self.assertFalse(
            any(
                q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for q in queries
            )
        )
        self.assertEqual(before, list(FixedRate.objects.order_by("pk").values()))
        self.assertIn("Bike=115; Car=80; total=195", output)
        self.assertIn("EXTRA ROW COUNT: 2", output)
        self.assertIn('"selected_extra": true', output)
        self.assertIn('"overlaps_approved_general_route": false', output)
        self.assertIn("Bike/Car extras same ordered route: True", output)
        self.assertIn('"origin": "Madina"', output)
        self.assertIn('"destination": "Adenta"', output)
        for rate in FixedRate.objects.filter(reason="Diagnostic test extra route"):
            self.assertIn(str(rate.pk), output)

    def test_inactive_zones_make_extra_inapplicable_to_resolver(self):
        OperationalZone.objects.create(name="Extra Origin")
        self.extra(origin_name="Extra Origin")
        OperationalZone.objects.filter(name="Extra Origin").update(active=False)
        output = self.report()
        self.assertIn('"selected_extra": false', output)
        self.assertIn('"origin_active": false', output)

    def test_apply_argument_is_rejected(self):
        with self.assertRaises(TypeError):
            call_command("diagnose_general_fixedrate_extras", apply=True)

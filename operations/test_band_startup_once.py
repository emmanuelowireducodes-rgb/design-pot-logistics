import os
from contextlib import ExitStack
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

import pricing_band_startup as startup

from .models import User


@override_settings(PRODUCTION=True, DEBUG=False)
class BandStartupOnceTests(TestCase):
    def setUp(self):
        User.objects.create_user("verified-admin", role="admin")
        self.empty = {
            "conflicts": [],
            "bands_identical": 0,
            "bands_to_create": 9,
            "assignments_matching": 0,
            "assignments_to_assign": 114,
            "identical_matrix_rates": [],
            "matrix_to_create": [("bike",)] * 45 + [("car",)] * 28,
            "hi_car_unset": 17,
            "zones": {
                "missing": [],
                "ambiguous": [],
                "duplicate_assignments": [],
                "duplicate_config_labels": 0,
            },
        }
        self.full = {
            **self.empty,
            "bands_identical": 9,
            "bands_to_create": 0,
            "assignments_matching": 114,
            "assignments_to_assign": 0,
            "identical_matrix_rates": [None] * 73,
            "matrix_to_create": [],
        }

    def mocks(self, previews):
        stack = ExitStack()
        stack.enter_context(
            patch.dict(os.environ, {"RENDER_GENERAL_PRICING_APPLY_ACTOR": "verified-admin"})
        )
        conn = stack.enter_context(patch.object(startup, "connection"))
        conn.vendor = "postgresql"
        stack.enter_context(patch.object(startup, "preview_rollout", side_effect=previews))
        stack.enter_context(patch.object(startup, "protected_state", return_value={"same": True}))
        stack.enter_context(patch.object(startup, "exact_rate_report"))
        stack.enter_context(patch.object(startup, "route_report"))
        self.commands = stack.enter_context(patch.object(startup, "call_command"))
        return stack

    def test_empty_approved_state_applies_once_then_previews(self):
        with self.mocks([self.empty, self.full]):
            startup.run_rollout()
        self.assertEqual(self.commands.call_count, 3)
        self.assertEqual(
            self.commands.call_args_list[1].kwargs, {"apply": True, "actor": "verified-admin"}
        )
        self.assertEqual(self.commands.call_args_list[2].kwargs, {})

    def test_restart_skips_apply_entirely(self):
        with self.mocks([self.full, self.full]):
            startup.run_rollout()
        self.assertTrue(all(not call.kwargs for call in self.commands.call_args_list))

    def test_changed_preview_refuses_apply(self):
        with self.mocks([{**self.empty, "bands_to_create": 8}]):
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.assertEqual(self.commands.call_count, 1)

    def test_invalid_actor_refuses_before_any_command(self):
        User.objects.filter(username="verified-admin").update(is_active=False)
        with self.mocks([]):
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.commands.assert_not_called()

    def test_failed_post_preview_refuses_further_writes(self):
        with self.mocks([self.empty, self.empty]):
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.assertEqual(self.commands.call_count, 3)

    def test_local_database_cannot_apply(self):
        with self.assertRaises(CommandError):
            startup.run_rollout()

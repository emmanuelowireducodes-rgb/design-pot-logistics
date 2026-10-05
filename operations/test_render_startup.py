import subprocess
import sys
from unittest.mock import call, patch

from django.test import SimpleTestCase

import render_startup


class RenderStartupTests(SimpleTestCase):
    def test_runs_migrations_apply_and_verification_before_web_command(self):
        web_command = ["gunicorn", "config.wsgi:application", "--workers", "3"]

        with patch("render_startup.subprocess.run") as run, patch(
            "render_startup.os.execvp"
        ) as execvp:
            render_startup.run_startup(web_command, actor="approved-admin")

        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [sys.executable, "manage.py", "migrate", "--noinput"],
                    cwd=render_startup.PROJECT_ROOT,
                    check=True,
                ),
                call(
                    [sys.executable, "manage.py", "import_general_dpot_prices", "--apply", "--actor", "approved-admin"],
                    cwd=render_startup.PROJECT_ROOT,
                    check=True,
                ),
                call(
                    [sys.executable, "manage.py", "import_general_dpot_prices"],
                    cwd=render_startup.PROJECT_ROOT,
                    check=True,
                ),
            ],
        )
        self.assertIn("--apply", run.call_args_list[1].args[0])
        self.assertNotIn("--apply", run.call_args_list[2].args[0])
        execvp.assert_called_once_with(web_command[0], web_command)

    def test_failed_pre_start_command_never_starts_web_server(self):
        with patch(
            "render_startup.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "preview"),
        ) as run, patch("render_startup.os.execvp") as execvp:
            with self.assertRaises(subprocess.CalledProcessError):
                render_startup.run_startup(["gunicorn", "config.wsgi:application"], actor="approved-admin")

        run.assert_called_once()
        execvp.assert_not_called()

    def test_requires_existing_web_command(self):
        with patch("render_startup.subprocess.run") as run, patch(
            "render_startup.os.execvp"
        ) as execvp:
            with self.assertRaises(ValueError):
                render_startup.run_startup([], actor="approved-admin")

        run.assert_not_called()
        execvp.assert_not_called()

    def test_missing_admin_actor_fails_before_any_command(self):
        with patch("render_startup.subprocess.run") as run, patch("render_startup.os.execvp") as execvp:
            with self.assertRaises(ValueError):
                render_startup.run_startup(["gunicorn", "config.wsgi:application"])

        run.assert_not_called()
        execvp.assert_not_called()

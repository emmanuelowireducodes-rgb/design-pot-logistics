import subprocess
import sys
from unittest.mock import call, patch

from django.test import SimpleTestCase

import render_startup


class RenderStartupTests(SimpleTestCase):
    def test_runs_migrations_and_read_only_preview_before_web_command(self):
        web_command = ["gunicorn", "config.wsgi:application", "--workers", "3"]

        with patch("render_startup.subprocess.run") as run, patch(
            "render_startup.os.execvp"
        ) as execvp:
            render_startup.run_startup(web_command)

        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [sys.executable, "manage.py", "migrate", "--noinput"],
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
        self.assertNotIn("--apply", run.call_args_list[1].args[0])
        execvp.assert_called_once_with(web_command[0], web_command)

    def test_failed_pre_start_command_never_starts_web_server(self):
        with patch(
            "render_startup.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "preview"),
        ) as run, patch("render_startup.os.execvp") as execvp:
            with self.assertRaises(subprocess.CalledProcessError):
                render_startup.run_startup(["gunicorn", "config.wsgi:application"])

        run.assert_called_once()
        execvp.assert_not_called()

    def test_requires_existing_web_command(self):
        with patch("render_startup.subprocess.run") as run, patch(
            "render_startup.os.execvp"
        ) as execvp:
            with self.assertRaises(ValueError):
                render_startup.run_startup([])

        run.assert_not_called()
        execvp.assert_not_called()

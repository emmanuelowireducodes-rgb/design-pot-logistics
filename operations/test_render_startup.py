import subprocess
import sys
from unittest.mock import call, patch

from django.test import SimpleTestCase

import render_startup


class RenderStartupTests(SimpleTestCase):
    def test_runs_migrations_and_both_previews_before_web_command(self):
        web_command = ["gunicorn", "config.wsgi:application", "--workers", "3"]

        with (
            patch("render_startup.subprocess.run") as run,
            patch("render_startup.os.execvp") as execvp,
        ):
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
                call(
                    [sys.executable, "manage.py", "apply_general_pricing_bands"],
                    cwd=render_startup.PROJECT_ROOT,
                    check=True,
                ),
                call(
                    [sys.executable, "pricing_band_startup.py"],
                    cwd=render_startup.PROJECT_ROOT,
                    check=True,
                ),
            ],
        )
        for command in run.call_args_list:
            self.assertNotIn("--apply", command.args[0])
            self.assertNotIn("--actor", command.args[0])
        execvp.assert_called_once_with(web_command[0], web_command)

    def test_web_starts_only_after_every_command_succeeds(self):
        order = []
        with (
            patch(
                "render_startup.subprocess.run",
                side_effect=lambda *args, **kwargs: order.append("preview"),
            ),
            patch("render_startup.os.execvp", side_effect=lambda *args: order.append("web")),
        ):
            render_startup.run_startup(["gunicorn", "config.wsgi:application"])
        self.assertEqual(order, ["preview", "preview", "preview", "preview", "web"])

    def test_failed_pre_start_command_never_starts_web_server(self):
        for failure_index in range(4):
            with self.subTest(failure_index=failure_index):
                results = [None] * failure_index + [subprocess.CalledProcessError(1, "preview")]
                with (
                    patch("render_startup.subprocess.run", side_effect=results) as run,
                    patch("render_startup.os.execvp") as execvp,
                ):
                    with self.assertRaises(subprocess.CalledProcessError):
                        render_startup.run_startup(["gunicorn", "config.wsgi:application"])
                self.assertEqual(run.call_count, failure_index + 1)
                execvp.assert_not_called()

    def test_requires_existing_web_command(self):
        with (
            patch("render_startup.subprocess.run") as run,
            patch("render_startup.os.execvp") as execvp,
        ):
            with self.assertRaises(ValueError):
                render_startup.run_startup([])

        run.assert_not_called()
        execvp.assert_not_called()

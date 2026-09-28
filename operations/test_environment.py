import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from config.environment import load_local_environment


class LocalEnvironmentTests(SimpleTestCase):
    def test_local_env_file_loads_in_development(self):
        key = "DPOT_TEST_LOCAL_ENV_VALUE"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{key}=loaded-from-file\n", encoding="utf-8")
            with patch.dict(os.environ):
                os.environ.pop(key, None)
                self.assertTrue(load_local_environment(env_path, production=False))
                self.assertEqual(os.environ[key], "loaded-from-file")

    def test_process_environment_value_takes_precedence(self):
        key = "DPOT_TEST_ENV_PRECEDENCE"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{key}=file-value\n", encoding="utf-8")
            with patch.dict(os.environ, {key: "process-value"}):
                load_local_environment(env_path, production=False)
                self.assertEqual(os.environ[key], "process-value")

    def test_missing_env_file_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ):
                self.assertFalse(
                    load_local_environment(Path(directory) / ".env", production=False)
                )

    def test_production_does_not_load_or_depend_on_env_file(self):
        key = "DPOT_TEST_PRODUCTION_ENV_VALUE"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{key}=file-value\n", encoding="utf-8")
            with patch.dict(os.environ):
                os.environ.pop(key, None)
                self.assertFalse(load_local_environment(env_path, production=True))
                self.assertNotIn(key, os.environ)

    def test_loading_does_not_log_secret_values(self):
        key = "DPOT_TEST_ENV_SECRET"
        secret = "test-only-secret-must-not-be-logged"
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(f"{key}={secret}\n", encoding="utf-8")
            output = StringIO()
            errors = StringIO()
            with patch.dict(os.environ):
                os.environ.pop(key, None)
                with redirect_stdout(output), redirect_stderr(errors):
                    load_local_environment(env_path, production=False)
                self.assertEqual(os.environ[key], secret)
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, errors.getvalue())

"""Run deployment checks before the Render-configured web server command."""

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PRE_START_COMMANDS = (
    ("manage.py", "migrate", "--noinput"),
    ("manage.py", "import_general_dpot_prices"),
)


def run_startup(web_command):
    if not web_command:
        raise ValueError("Pass the existing web server command after --.")

    for command in PRE_START_COMMANDS:
        rendered = " ".join(command)
        print(f"==> Running: python {rendered}", flush=True)
        subprocess.run([sys.executable, *command], cwd=PROJECT_ROOT, check=True)

    print(f"==> Starting web server: {' '.join(web_command)}", flush=True)
    os.execvp(web_command[0], web_command)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    run_startup(arguments)


if __name__ == "__main__":
    main()

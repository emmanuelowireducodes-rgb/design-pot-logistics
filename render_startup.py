"""Temporary Render pre-start sequence for the one-time General Dpot import."""

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
def startup_commands(actor):
    if not actor:
        raise ValueError("Set RENDER_GENERAL_PRICING_APPLY_ACTOR for the one-time apply.")
    return (
        ("manage.py", "migrate", "--noinput"),
        ("manage.py", "import_general_dpot_prices", "--apply", "--actor", actor),
        ("manage.py", "import_general_dpot_prices"),
    )


def run_startup(web_command, actor=None):
    if not web_command:
        raise ValueError("Pass the existing web server command after --.")

    for command in startup_commands(actor):
        rendered = " ".join(command)
        print(f"==> TEMPORARY ONE-TIME PRICING APPLY: python {rendered}", flush=True)
        subprocess.run([sys.executable, *command], cwd=PROJECT_ROOT, check=True)

    print(f"==> Starting web server: {' '.join(web_command)}", flush=True)
    os.execvp(web_command[0], web_command)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    run_startup(arguments, actor=os.environ.get("RENDER_GENERAL_PRICING_APPLY_ACTOR"))


if __name__ == "__main__":
    main()

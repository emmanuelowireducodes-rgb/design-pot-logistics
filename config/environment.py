from pathlib import Path


def load_local_environment(env_path: Path, *, production: bool) -> bool:
    """Load an optional local .env without overriding process variables."""
    if production:
        return False

    from dotenv import load_dotenv

    return load_dotenv(dotenv_path=env_path, override=False, verbose=False, encoding="utf-8")

"""Load the repository .env without replacing service-manager environment values."""
from pathlib import Path
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_environment(path: str | Path = ENV_FILE) -> None:
    load_dotenv(dotenv_path=path, override=False)


load_environment()

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("GOALKEEPER_DATA", PROJECT / "runtime")).resolve()
RUNS = DATA / "runs"
PRIVATE = DATA / "private"


def initialize() -> None:
    for directory in (DATA, RUNS, PRIVATE):
        directory.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)

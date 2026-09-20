
import os
import sqlite3
from pathlib import Path


def default_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path or default_path()))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

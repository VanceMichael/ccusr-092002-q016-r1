
import tempfile
import unittest
from pathlib import Path

from app import db
from app.main import dispatch, make_handler
from scripts.migrate import run_migrations


class HealthTest(unittest.TestCase):
    def test_health_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            run_migrations(path)
            conn = db.connect(path)
            try:
                status, body = dispatch("GET", "/health", {}, b"", conn)
            finally:
                conn.close()
            self.assertEqual(status, 200)
            self.assertEqual(body, {"status": "ok"})

    def test_handler_factory(self) -> None:
        Handler = make_handler(Path("/tmp/unused.sqlite3"))
        self.assertTrue(callable(Handler.do_GET))


if __name__ == "__main__":
    unittest.main()

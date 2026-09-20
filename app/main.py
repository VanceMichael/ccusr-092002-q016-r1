import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from app import service
from scripts.migrate import run_migrations

# 写操作由科研负责人团队、验收人员承担；实验记录与样本交接允许研究机构登记。
WRITE_ROLES = {service.ROLE_COORDINATOR, service.ROLE_INSPECTOR}
FIELD_ROLES = {service.ROLE_COORDINATOR, service.ROLE_INSPECTOR, service.ROLE_RESEARCHER}


class Handler(BaseHTTPRequestHandler):
    server_version = "SeaTrialAcceptance/1.0"

    # ------------------------------------------------------------ 基础工具

    def _send_json(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise service.DomainError(400, f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise service.DomainError(400, "请求体必须是 JSON 对象")
        return payload

    def _headers(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    def _execute(self, allowed_roles: set[str], fn) -> None:
        conn = sqlite3.connect(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            service.require_role(self._headers(), allowed_roles)
            result = fn(conn)
            conn.commit()
            self._send_json(200, result if result is not None else {"ok": True})
        except service.DomainError as exc:
            conn.rollback()
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - 统一兜底，避免栈泄露给调用方
            conn.rollback()
            self._send_json(500, {"error": f"服务器内部错误：{exc}"})
        finally:
            conn.close()

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        m = re.fullmatch(r"/voyages/([^/]+)/delivery", path)
        if m:
            voyage_id = m.group(1)
            self._execute(
                {service.ROLE_SHIPOWNER, service.ROLE_COORDINATOR, service.ROLE_INSPECTOR},
                lambda conn: service.delivery_report(conn, voyage_id),
            )
            return

        m = re.fullmatch(r"/voyages/([^/]+)/acceptance", path)
        if m:
            voyage_id = m.group(1)
            self._execute(
                {service.ROLE_INSPECTOR, service.ROLE_COORDINATOR},
                lambda conn: service.acceptance_report(conn, voyage_id),
            )
            return

        m = re.fullmatch(r"/samples/([^/]+)/provenance", path)
        if m:
            sample_ref = m.group(1)

            def action(conn: sqlite3.Connection) -> dict:
                _, institution_ref = service.require_role(
                    self._headers(), {service.ROLE_RESEARCHER}
                )
                return service.sample_provenance(conn, sample_ref, institution_ref)

            self._execute(
                {service.ROLE_RESEARCHER},
                lambda conn: action(conn),
            )
            return

        m = re.fullmatch(r"/samples/([^/]+)/readings", path)
        if m:
            sample_ref = m.group(1)

            def action(conn: sqlite3.Connection) -> dict:
                _, institution_ref = service.require_role(
                    self._headers(), {service.ROLE_RESEARCHER}
                )
                return service.sample_readings(conn, sample_ref, institution_ref)

            self._execute(
                {service.ROLE_RESEARCHER},
                lambda conn: action(conn),
            )
            return

        self._send_json(404, {"error": f"路径不存在：{path}"})

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        routes: list[tuple[re.Pattern, set[str], Callable]] = [
            (re.compile(r"^/voyages$"), WRITE_ROLES,
             lambda p, m, conn: service.create_voyage(conn, p)),
            (re.compile(r"^/voyages/([^/]+)/legs$"), WRITE_ROLES,
             lambda p, m, conn: service.create_leg(conn, m.group(1), p)),
            (re.compile(r"^/voyages/([^/]+)/subjects$"), WRITE_ROLES,
             lambda p, m, conn: service.create_subject(conn, m.group(1), p)),
            (re.compile(r"^/instruments$"), WRITE_ROLES,
             lambda p, m, conn: service.create_instrument(conn, p)),
            (re.compile(r"^/instruments/([^/]+)/certificates$"), WRITE_ROLES,
             lambda p, m, conn: service.add_certificate(conn, m.group(1), p)),
            (re.compile(r"^/subjects/([^/]+)/instruments$"), WRITE_ROLES,
             lambda p, m, conn: service.link_instrument(conn, m.group(1), p)),
            (re.compile(r"^/subjects/([^/]+)/windows$"), FIELD_ROLES,
             lambda p, m, conn: service.add_window(conn, m.group(1), p)),
            (re.compile(r"^/subjects/([^/]+)/runs$"), FIELD_ROLES,
             lambda p, m, conn: service.add_run(conn, m.group(1), p)),
            (re.compile(r"^/subjects/([^/]+)/decision$"), {service.ROLE_INSPECTOR},
             lambda p, m, conn: service.decide_subject(conn, m.group(1), p)),
            (re.compile(r"^/runs/([^/]+)/readings$"), FIELD_ROLES,
             lambda p, m, conn: service.add_reading(conn, m.group(1), p)),
            (re.compile(r"^/runs/([^/]+)/samples$"), FIELD_ROLES,
             lambda p, m, conn: service.add_sample(conn, m.group(1), p)),
            (re.compile(r"^/samples/([^/]+)/handovers$"), FIELD_ROLES,
             lambda p, m, conn: service.add_handover(conn, m.group(1), p)),
            (re.compile(r"^/samples/([^/]+)/access-grants$"), {service.ROLE_COORDINATOR},
             lambda p, m, conn: service.grant_sample_access(conn, m.group(1), p)),
            (re.compile(r"^/readings/([^/]+)/review$"), FIELD_ROLES,
             lambda p, m, conn: service.review_reading(conn, m.group(1), p)),
        ]
        for pattern, roles, handler in routes:
            match = pattern.fullmatch(path)
            if match:
                try:
                    payload = self._read_body()
                except service.DomainError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                    return
                self._execute(roles, lambda conn: handler(payload, match, conn))
                return
        self._send_json(404, {"error": f"路径不存在：{path}"})

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    run_migrations(Path(database_path))
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

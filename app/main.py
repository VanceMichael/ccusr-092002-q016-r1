
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app import db
from app import service as svc
from scripts.migrate import run_migrations


# 角色到可执行动作的映射
PERMISSIONS = {
    "scientist": {
        "write", "grant", "report",
    },
    "acceptance": {
        "complete", "validate", "report",
    },
    "owner": {
        "delivery",
    },
    "researcher": {
        "trace",
    },
}


def authenticate(conn, headers) -> dict:
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise svc.DomainError(401, "缺少 Bearer 令牌")
    token = auth[len("Bearer "):].strip()
    row = conn.execute("SELECT * FROM access_tokens WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise svc.DomainError(401, "令牌无效或已注销")
    return dict(row)


def authorize(user: dict, action: str) -> None:
    if action not in PERMISSIONS.get(user["role"], set()):
        raise svc.DomainError(403, f"角色 {user['role']} 无权执行 {action}")


def dispatch(method: str, target: str, headers: dict, raw_body: bytes, conn) -> tuple[int, dict]:
    parsed = urlparse(target)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}

    user = authenticate(conn, headers)
    body: dict = {}
    if raw_body:
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise svc.DomainError(400, "请求体必须是 UTF-8 JSON")
        if not isinstance(body, dict):
            raise svc.DomainError(400, "请求体必须是 JSON 对象")

    def match(pattern: str):
        return re.fullmatch(pattern, path)

    if method == "POST":
        m = match(r"/voyages")
        if m:
            authorize(user, "write")
            return 201, svc.create_voyage(conn, body)
        m = match(r"/voyages/([^/]+)/legs")
        if m:
            authorize(user, "write")
            body["voyage_id"] = m.group(1)
            return 201, svc.add_leg(conn, body)
        m = match(r"/instruments")
        if m:
            authorize(user, "write")
            return 201, svc.create_instrument(conn, body)
        m = match(r"/certificates")
        if m:
            authorize(user, "write")
            return 201, svc.add_certificate(conn, body)
        m = match(r"/subjects")
        if m:
            authorize(user, "write")
            return 201, svc.create_subject(conn, body)
        m = match(r"/observations")
        if m:
            authorize(user, "write")
            return 201, svc.add_observation(conn, body)
        m = match(r"/experiments")
        if m:
            authorize(user, "write")
            return 201, svc.add_experiment(conn, body)
        m = match(r"/experiments/([^/]+)/readings")
        if m:
            authorize(user, "write")
            body["experiment_run_id"] = m.group(1)
            return 201, svc.add_readings(conn, body)
        m = match(r"/readings/(\d+)/review")
        if m:
            authorize(user, "write")
            return 200, svc.review_reading(conn, int(m.group(1)), body)
        m = match(r"/samples")
        if m:
            authorize(user, "write")
            return 201, svc.add_sample(conn, body)
        m = match(r"/samples/([^/]+)/handovers")
        if m:
            authorize(user, "write")
            return 201, svc.add_handover(conn, m.group(1), body)
        m = match(r"/grants")
        if m:
            authorize(user, "grant")
            return 201, svc.grant_sample_access(conn, body)
        m = match(r"/subjects/([^/]+)/complete")
        if m:
            authorize(user, "complete")
            body["completed_by"] = body.get("completed_by", user["party_ref"])
            return 200, svc.complete_subject(conn, m.group(1), body)
        m = match(r"/subjects/([^/]+)/fail")
        if m:
            authorize(user, "complete")
            return 200, svc.fail_subject(conn, m.group(1), body)
        m = match(r"/validations")
        if m:
            authorize(user, "validate")
            return 200, svc.record_validation(conn, body)

    if method == "GET":
        m = match(r"/voyages/([^/]+)/report")
        if m:
            authorize(user, "report")
            return 200, svc.voyage_report(conn, m.group(1))
        m = match(r"/voyages/([^/]+)/delivery")
        if m:
            authorize(user, "delivery")
            return 200, svc.owner_delivery(conn, m.group(1))
        m = match(r"/samples/trace")
        if m:
            authorize(user, "trace")
            org_ref = query.get("org", [user["party_ref"]])[0]
            if org_ref != user["party_ref"]:
                raise svc.DomainError(403, "研究机构只能追溯本机构被授权的样本")
            return 200, svc.researcher_samples(conn, org_ref)

    raise svc.DomainError(404, f"未找到路由：{method} {path}")


def make_handler(database_path):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length else b""
            conn = db.connect(database_path)
            try:
                status, payload = dispatch(
                    self.command, self.path, self.headers, raw_body, conn
                )
            except svc.DomainError as exc:
                conn.rollback()
                status, payload = exc.status, {"error": exc.message, "details": exc.details}
            except Exception:
                conn.rollback()
                status, payload = 500, {"error": "服务器内部错误", "details": []}
            finally:
                conn.close()
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    database_path = db.default_path()
    run_migrations(database_path)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(database_path)).serve_forever()


if __name__ == "__main__":
    main()


import json
import tempfile
import unittest
from pathlib import Path

from scripts.migrate import run_migrations
from app import db
from app.main import dispatch
from app.service import DomainError

TOKENS = {
    "scientist": "Bearer SCI-T",
    "acceptance": "Bearer ACC-T",
    "owner": "Bearer OWN-T",
    "researcher": "Bearer RI-T",
    "researcher2": "Bearer RI2-T",
}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        run_migrations(self.db_path)
        self.conn = db.connect(self.db_path)
        self.conn.executemany(
            "INSERT INTO access_tokens(token, role, party_ref, label) VALUES (?,?,?,?)",
            [("SCI-T", "scientist", "SCI-TEAM", "科研团队"),
             ("ACC-T", "acceptance", "ACC-LI", "验收"),
             ("OWN-T", "owner", "OWNER", "船东"),
             ("RI-T", "researcher", "RI-A", "机构A"),
             ("RI2-T", "researcher", "RI-B", "机构B")],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    # ---- 辅助 ----
    def call(self, method, path, role=None, payload=None, token=None, raw=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = token
        elif role is not None:
            headers["Authorization"] = TOKENS[role]
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return dispatch(method, path, headers, data if (payload is not None or raw is not None) else b"",
                        self.conn)

    def err(self, method, path, role, payload=None):
        with self.assertRaises(DomainError) as ctx:
            self.call(method, path, role, payload)
        return ctx.exception

    def voyage_leg(self, vid="V1", dep="2026-09-10T08:00:00+08:00"):
        self.call("POST", "/voyages", "scientist",
                  {"id": vid, "vessel_ref": "VES-1", "departure_at": dep})
        self.call("POST", f"/voyages/{vid}/legs", "scientist",
                  {"started_at": "2026-09-10T09:00:00+08:00",
                   "ended_at": "2026-09-10T18:00:00+08:00"})

    def instrument_cert(self, iid, vid):
        self.call("POST", "/instruments", "scientist",
                  {"id": iid, "name": iid, "category": "综合"})
        self.call("POST", "/certificates", "scientist",
                  {"id": f"C-{iid}-{vid}", "instrument_id": iid, "voyage_id": vid,
                   "issuer": "计量站", "calibrated_at": "2026-09-08T10:00:00+08:00",
                   "valid_until": "2027-09-08T10:00:00+08:00"})

    def full_subject(self, sid, vid, iid, leg_id=1):
        self.call("POST", "/subjects", "scientist",
                  {"id": sid, "voyage_id": vid, "name": sid, "instrument_ids": [iid]})
        self.call("POST", "/observations", "scientist",
                  {"subject_id": sid, "leg_id": leg_id, "instrument_id": iid,
                   "started_at": "2026-09-10T10:00:00+08:00",
                   "ended_at": "2026-09-10T11:00:00+08:00", "observer": "王"})
        self.call("POST", "/experiments", "scientist",
                  {"id": f"R-{sid}", "subject_id": sid, "leg_id": leg_id,
                   "instrument_id": iid, "protocol_ref": "P-v1",
                   "parameters": {"k": "v"}, "operator": "王",
                   "ran_at": "2026-09-10T10:30:00+08:00"})

    # ---- 1. 健康检查与鉴权 ----
    def test_health_open(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_and_bad_token(self):
        with self.assertRaises(DomainError) as ctx:
            dispatch("GET", "/voyages/V1/report", {}, b"", self.conn)
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(DomainError) as ctx:
            self.call("GET", "/voyages/V1/report", token="Bearer NOPE")
        self.assertEqual(ctx.exception.status, 401)

    def test_role_separation(self):
        # 船东不能看验收明细，研究机构不能写，科研团队不能出验收结论
        self.assertEqual(self.err("GET", "/voyages/V1/report", "owner").status, 403)
        self.assertEqual(self.err("POST", "/voyages", "researcher", {}).status, 403)
        self.assertEqual(self.err("POST", "/validations", "scientist", {}).status, 403)

    # ---- 2. 跨航次防冒用 ----
    def test_cannot_reuse_other_voyage_certificate(self):
        self.voyage_leg("V1")
        self.voyage_leg("V2", "2026-10-01T08:00:00+08:00")
        self.instrument_cert("I1", "V1")  # 证书只属于 V1
        self.call("POST", f"/voyages/V2/legs", "scientist",
                  {"started_at": "2026-10-01T09:00:00+08:00"})
        self.full_subject("S2", "V2", "I1", leg_id=2)
        # V2 科目证据齐全但缺本航次合格证书 -> 不能完成
        exc = self.err("POST", "/subjects/S2/complete", "acceptance", {})
        self.assertEqual(exc.status, 422)
        self.assertTrue(any("校准证书" in b for b in exc.details))
        # 上报给 V1 的验收结论同样不能用于 V2
        status, report = self.call("GET", "/voyages/V2/report", "acceptance")
        inst = report["instruments"][0]
        self.assertEqual(inst["computed_result"], "待复测")
        self.assertTrue(inst["blocking_reasons"])

    def test_leg_from_other_voyage_rejected(self):
        self.voyage_leg("V1")
        self.voyage_leg("V2", "2026-10-01T08:00:00+08:00")
        self.call("POST", f"/voyages/V2/legs", "scientist",
                  {"started_at": "2026-10-01T09:00:00+08:00"})
        self.instrument_cert("I1", "V2")
        self.call("POST", "/subjects", "scientist",
                  {"id": "SX", "voyage_id": "V2", "name": "sx", "instrument_ids": ["I1"]})
        # 把 V1 的航段（id=1）挂到 V2 科目上 -> 拒绝
        exc = self.err("POST", "/observations", "scientist",
                       {"subject_id": "SX", "leg_id": 1, "instrument_id": "I1",
                        "started_at": "2026-10-01T10:00:00+08:00", "observer": "王"})
        self.assertEqual(exc.status, 422)
        self.assertIn("跨航次", exc.message)

    def test_expired_certificate_rejected(self):
        self.voyage_leg("V1")
        self.call("POST", "/instruments", "scientist",
                  {"id": "I1", "name": "I1", "category": "x"})
        exc = self.err("POST", "/certificates", "scientist",
                       {"id": "C1", "instrument_id": "I1", "voyage_id": "V1",
                        "issuer": "站", "calibrated_at": "2025-01-01T00:00:00+08:00",
                        "valid_until": "2026-09-01T00:00:00+08:00"})
        self.assertEqual(exc.status, 422)

    # ---- 3. 异常读数与复核 ----
    def test_anomaly_must_be_reviewed_before_complete(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        self.call("POST", "/experiments/R-S1/readings", "scientist",
                  {"readings": [
                      {"metric": "温度", "raw_value": 99.9, "unit": "C",
                       "recorded_at": "2026-09-10T10:31:00+08:00",
                       "is_anomaly": True, "anomaly_reason": "瞬时跳变"}]})
        # 未复核 -> 不能完成
        exc = self.err("POST", "/subjects/S1/complete", "acceptance", {})
        self.assertTrue(any("复核" in b for b in exc.details))
        # 找到读数 id 并复核（保留原始值）
        rid = self.conn.execute(
            "SELECT id FROM readings WHERE metric='温度'").fetchone()["id"]
        status, body = self.call("POST", f"/readings/{rid}/review", "scientist",
                                 {"reviewed_by": "李复核", "review_result": "确认异常"})
        self.assertEqual(status, 200)
        raw = self.conn.execute("SELECT raw_value FROM readings WHERE id=?", (rid,)).fetchone()
        self.assertEqual(raw["raw_value"], "99.9")
        # 复核不可重复
        self.assertEqual(self.err("POST", f"/readings/{rid}/review", "scientist",
                                  {"reviewed_by": "他人", "review_result": "误报修正"}).status, 409)
        # 现在可以完成
        status, body = self.call("POST", "/subjects/S1/complete", "acceptance", {})
        self.assertEqual(body["computed_result"], "通过")

    def test_anomaly_payload_requires_reviewer_pair(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        exc = self.err("POST", "/experiments/R-S1/readings", "scientist",
                       {"readings": [{"metric": "m", "raw_value": 1,
                                      "recorded_at": "2026-09-10T10:31:00+08:00",
                                      "is_anomaly": True, "reviewed_by": "李"}]})
        self.assertEqual(exc.status, 400)

    # ---- 4. 正常闭环：完成、验收、船东视图 ----
    def test_happy_path_delivery_and_report(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        self.call("POST", "/experiments/R-S1/readings", "scientist",
                  {"readings": [{"metric": "值", "raw_value": 1.0,
                                 "recorded_at": "2026-09-10T10:31:00+08:00"}]})
        self.call("POST", "/subjects/S1/complete", "acceptance", {})
        decided = "2026-09-10T21:00:00+08:00"
        status, val = self.call("POST", "/validations", "acceptance",
                                {"instrument_id": "I1", "voyage_id": "V1", "leg_id": 1,
                                 "result": "通过", "decided_by": "ACC-LI",
                                 "decided_at": decided})
        self.assertEqual(status, 200)
        # 验收报告：仪器验证航段
        status, report = self.call("GET", "/voyages/V1/report", "acceptance")
        inst = report["instruments"][0]
        self.assertEqual(inst["computed_result"], "通过")
        self.assertEqual(inst["verified_in_legs"][0]["seq"], 1)
        self.assertEqual(inst["acceptance_decision"]["result"], "通过")
        # 船东视图只有交付结论
        status, delivery = self.call("GET", "/voyages/V1/delivery", "owner")
        item = delivery["instruments"][0]
        self.assertTrue(item["meets_delivery_conditions"])
        self.assertNotIn("verified_in_legs", item)
        self.assertNotIn("blocking_reasons", item)

    def test_retest_validation_requires_reason(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        exc = self.err("POST", "/validations", "acceptance",
                       {"instrument_id": "I1", "voyage_id": "V1", "result": "待复测",
                        "decided_by": "ACC-LI", "decided_at": "2026-09-10T21:00:00+08:00"})
        self.assertEqual(exc.status, 400)

    # ---- 5. 样本授权与研究机构溯源 ----
    def test_researcher_trace_scoped_by_grant(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        self.call("POST", "/samples", "scientist",
                  {"id": "SMP-1", "subject_id": "S1", "experiment_run_id": "R-S1",
                   "storage_instrument_id": "I1",
                   "collected_at": "2026-09-10T10:40:00+08:00", "collected_by": "王",
                   "metadata_json": {"站点": "A07"}})
        self.call("POST", "/samples/SMP-1/handovers", "scientist",
                  {"from_party": "采样组", "to_party": "接收员",
                   "handed_at": "2026-09-10T20:00:00+08:00"})
        # 未授权：两个机构都看不到
        status, body = self.call("GET", "/samples/trace", "researcher")
        self.assertEqual(body["samples"], [])
        # 授权 RI-A 单样本
        self.call("POST", "/grants", "scientist",
                  {"org_ref": "RI-A", "sample_id": "SMP-1", "granted_by": "SCI-TEAM"})
        status, body = self.call("GET", "/samples/trace", "researcher")
        self.assertEqual(body["count"], 1)
        sample = body["samples"][0]
        self.assertEqual(sample["sample_id"], "SMP-1")
        self.assertEqual(sample["experiment"]["protocol_ref"], "P-v1")
        self.assertEqual(sample["handovers"][0]["to_party"], "接收员")
        # RI-B 仍看不到
        status, body = self.call("GET", "/samples/trace", "researcher2")
        self.assertEqual(body["samples"], [])
        # 不能冒充其他机构查询
        self.assertEqual(
            self.err("GET", "/samples/trace?org=RI-A", "researcher2").status, 403)

    def test_subject_level_grant_covers_future_samples(self):
        self.voyage_leg("V1")
        self.instrument_cert("I1", "V1")
        self.full_subject("S1", "V1", "I1")
        self.call("POST", "/grants", "scientist",
                  {"org_ref": "RI-A", "subject_id": "S1", "granted_by": "SCI-TEAM"})
        self.call("POST", "/samples", "scientist",
                  {"id": "SMP-9", "subject_id": "S1", "storage_instrument_id": "I1",
                   "collected_at": "2026-09-10T10:40:00+08:00", "collected_by": "王"})
        status, body = self.call("GET", "/samples/trace", "researcher")
        self.assertEqual({s["sample_id"] for s in body["samples"]}, {"SMP-9"})

    # ---- 6. 输入校验 ----
    def test_time_must_have_offset(self):
        exc = self.err("POST", "/voyages", "scientist",
                       {"id": "V9", "vessel_ref": "X", "departure_at": "2026-09-10 08:00:00"})
        self.assertEqual(exc.status, 400)

    def test_unknown_route_and_bad_json(self):
        self.assertEqual(self.err("GET", "/nope", "owner").status, 404)
        with self.assertRaises(DomainError) as ctx:
            self.call("POST", "/voyages", "scientist", raw=b"{not json")
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main()

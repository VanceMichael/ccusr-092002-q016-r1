
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.main import Handler
from scripts.migrate import run_migrations


def _headers(role: str, actor: str) -> dict:
    return {"Content-Type": "application/json", "X-Actor-Role": role, "X-Actor-Ref": actor}


class AcceptanceFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.sqlite3")
        run_migrations(Path(self.db_path))
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self.db_path
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmpdir.cleanup()
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env

    # ------------------------------------------------------------ helpers

    def request(self, method: str, path: str, role: str = "", actor: str = "",
                body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = _headers(role, actor) if role else {"Content-Type": "application/json"}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def post(self, path: str, role: str, actor: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", path, role, actor, body)

    def get(self, path: str, role: str, actor: str) -> tuple[int, dict]:
        return self.request("GET", path, role, actor)

    def _seed_voyage(self, voyage_id: str = "V1") -> None:
        self.post("/voyages", "coordinator", "COORD-1", {
            "voyage_id": voyage_id, "vessel_ref": "VESSEL-DEMO",
            "title": "第一次海试", "sea_area_ref": "AREA-A",
            "departure_at": "2026-09-10T08:00:00+08:00",
        })
        self.post(f"/voyages/{voyage_id}/legs", "coordinator", "COORD-1", {
            "leg_id": f"{voyage_id}-L1", "name": "出港航段",
            "started_at": "2026-09-10T09:00:00+08:00",
            "ended_at": "2026-09-10T18:00:00+08:00",
        })

    def _register_instrument(self, instrument_id: str, name: str,
                             valid_from: str, valid_until: str) -> None:
        self.post("/instruments", "coordinator", "COORD-1", {
            "instrument_id": instrument_id, "vessel_ref": "VESSEL-DEMO",
            "name": name, "yard_record_ref": f"YARD-{instrument_id}",
            "installed_at": "2026-09-09T10:00:00+08:00",
        })
        self.post(f"/instruments/{instrument_id}/certificates", "inspector", "INSP-1", {
            "cert_id": f"CERT-{instrument_id}", "cert_ref": f"CAL-{instrument_id}",
            "issued_by": "计量站", "calibrated_at": valid_from, "valid_until": valid_until,
        })

    def _fully_evidence_subject(self, voyage_id: str, subject_id: str, instrument_id: str,
                                sample_ref: str | None = None, anomaly: bool = False) -> None:
        self.post(f"/voyages/{voyage_id}/subjects", "coordinator", "COORD-1", {
            "subject_id": subject_id, "code": subject_id, "name": f"科目-{subject_id}",
        })
        self.post(f"/subjects/{subject_id}/instruments", "coordinator", "COORD-1",
                  {"instrument_id": instrument_id})
        self.post(f"/subjects/{subject_id}/windows", "researcher", "RES-1", {
            "window_id": f"W-{subject_id}", "leg_id": f"{voyage_id}-L1",
            "started_at": "2026-09-10T10:00:00+08:00",
            "ended_at": "2026-09-10T12:00:00+08:00",
            "recorded_by": "RES-1", "note": "海上实际海况观测",
        })
        self.post(f"/subjects/{subject_id}/runs", "researcher", "RES-1", {
            "run_id": f"RUN-{subject_id}", "procedure_ref": "PROC-STD-01",
            "params": {"speed_kn": 8, "sea_state": 4},
            "operator_ref": "RES-1", "recorded_at": "2026-09-10T11:00:00+08:00",
            "reproducibility_note": "参数与程序编号可复现",
        })
        _, created = self.post(f"/runs/RUN-{subject_id}/readings", "researcher", "RES-1", {
            "reading_id": f"R-{subject_id}", "metric": "salinity",
            "raw_value": "34.71", "unit": "PSU",
            "reading_at": "2026-09-10T11:05:00+08:00",
            "anomaly": anomaly,
            "reviewed_by": "INSP-1" if anomaly else None,
            "review_note": "涌浪导致跳变，复核保留" if anomaly else None,
        })
        self.assertTrue(created["anomaly"] is anomaly)
        if sample_ref:
            self.post(f"/runs/RUN-{subject_id}/samples", "researcher", "RES-1", {
                "sample_id": f"S-{subject_id}", "sample_ref": sample_ref,
                "collected_at": "2026-09-10T11:10:00+08:00",
                "specimen_desc": "表层海水 2L", "condition_note": "冷藏 4℃",
            })
            self.post(f"/samples/S-{subject_id}/handovers", "researcher", "RES-1", {
                "handover_id": f"H-{subject_id}", "from_party": "走航组",
                "to_party": "冷藏样本舱管理员", "receiver_ref": "KEEP-1",
                "handed_at": "2026-09-10T11:30:00+08:00",
                "location": "样本舱", "condition_note": "冷链温度记录正常",
            })

    # ------------------------------------------------------------ tests

    def test_full_acceptance_flow_and_views(self) -> None:
        self._seed_voyage("V1")
        self._register_instrument("I-SAMPLER", "走航采样仪",
                                  "2026-08-01T00:00:00+08:00", "2027-08-01T00:00:00+08:00")
        self._register_instrument("I-FREEZER", "冷藏样本舱",
                                  "2026-08-01T00:00:00+08:00", "2027-08-01T00:00:00+08:00")
        self._register_instrument("I-GPS", "定位设备",
                                  "2026-08-01T00:00:00+08:00", "2027-08-01T00:00:00+08:00")
        self._fully_evidence_subject("V1", "SUB-SAMPLER", "I-SAMPLER",
                                     sample_ref="SAMPLE-001", anomaly=True)
        self._fully_evidence_subject("V1", "SUB-FREEZER", "I-FREEZER")
        self._fully_evidence_subject("V1", "SUB-GPS", "I-GPS")

        # 样本授权：仅授权机构 R-INST-A 可追溯 SAMPLE-001。
        status, body = self.post("/samples/S-SUB-SAMPLER/access-grants", "coordinator", "COORD-1", {
            "grant_id": "G-1", "institution_ref": "R-INST-A",
            "scope_note": "允许追溯样本来源与交接链", "granted_by": "COORD-1",
        })
        self.assertEqual(status, 200, body)

        status, body = self.get("/samples/SAMPLE-001/provenance", "researcher", "R-INST-A")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"]["voyage_id"], "V1")
        self.assertEqual(body["source"]["procedure_ref"], "PROC-STD-01")
        self.assertEqual(body["handovers"][0]["receiver_ref"], "KEEP-1")
        self.assertEqual(body["authorization_scope"], "允许追溯样本来源与交接链")

        # 原始读数与复核人对授权机构可见且保留原始值。
        status, readings = self.get("/samples/SAMPLE-001/readings", "researcher", "R-INST-A")
        self.assertEqual(status, 200, readings)
        self.assertEqual(readings["readings"][0]["raw_value"], "34.71")
        self.assertTrue(readings["readings"][0]["anomaly"])
        self.assertEqual(readings["readings"][0]["reviewed_by"], "INSP-1")

        # 未授权机构不能追溯，且船东根本不能访问样本视图。
        status, body = self.get("/samples/SAMPLE-001/provenance", "researcher", "R-INST-B")
        self.assertEqual(status, 403)
        status, body = self.get("/samples/SAMPLE-001/provenance", "shipowner", "OWNER-1")
        self.assertEqual(status, 403)

        # 证据齐备后验收人员判定合格。
        for subject_id in ("SUB-SAMPLER", "SUB-FREEZER", "SUB-GPS"):
            status, body = self.post(f"/subjects/{subject_id}/decision", "inspector", "INSP-1",
                                     {"status": "PASSED", "decided_by": "INSP-1"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["status"], "PASSED")

        # 船东交付视图：只看到满足交付条件与否，看不到读数/样本。
        status, delivery = self.get("/voyages/V1/delivery", "shipowner", "OWNER-1")
        self.assertEqual(status, 200, delivery)
        self.assertTrue(all(i["delivery_ready"] for i in delivery["instruments"]))
        self.assertEqual(len(delivery["instruments"]), 3)
        self.assertNotIn("readings", json.dumps(delivery, ensure_ascii=False))

        # 验收视图：指出每件仪器在哪段航行完成验证。
        status, acceptance = self.get("/voyages/V1/acceptance", "inspector", "INSP-1")
        self.assertEqual(status, 200, acceptance)
        gps = next(i for i in acceptance["instruments"] if i["instrument_id"] == "I-GPS")
        subject = gps["subjects"][0]
        self.assertEqual(subject["validated_in_legs"][0]["leg_id"], "V1-L1")
        self.assertEqual(subject["status"], "PASSED")

        # 船东不能查看验收明细（含航段与复测原因的验收视图）。
        status, _ = self.get("/voyages/V1/acceptance", "shipowner", "OWNER-1")
        self.assertEqual(status, 403)

    def test_anomaly_review_two_stage_flow(self) -> None:
        self._seed_voyage("V1")
        self._register_instrument("I-GPS", "定位设备",
                                  "2026-08-01T00:00:00+08:00", "2027-08-01T00:00:00+08:00")
        self._fully_evidence_subject("V1", "SUB-GPS", "I-GPS")

        # 海上先登记异常读数，允许暂无复核人，原始读数照实保留。
        status, body = self.post("/runs/RUN-SUB-GPS/readings", "researcher", "RES-1", {
            "reading_id": "R-BAD", "metric": "fix_quality", "raw_value": "0",
            "unit": "index", "reading_at": "2026-09-10T11:20:00+08:00",
            "anomaly": True,
        })
        self.assertEqual(status, 200, body)
        self.assertTrue(body["anomaly"])

        # 复核未补齐前，科目不允许判合格。
        status, body = self.post("/subjects/SUB-GPS/decision", "inspector", "INSP-1",
                                 {"status": "PASSED", "decided_by": "INSP-1"})
        self.assertEqual(status, 422)
        self.assertIn("未经复核", body["error"])

        # 非异常读数不允许走复核接口。
        status, body = self.post("/readings/R-SUB-GPS/review", "inspector", "INSP-1",
                                 {"reviewed_by": "INSP-1"})
        self.assertEqual(status, 409)

        # 靠港后补登复核人与复核说明。
        status, body = self.post("/readings/R-BAD/review", "inspector", "INSP-1", {
            "reviewed_by": "INSP-1", "review_note": "定位失锁原始记录保留，重测确认正常"})
        self.assertEqual(status, 200, body)

        # 复核补齐后可以判合格。
        status, body = self.post("/subjects/SUB-GPS/decision", "inspector", "INSP-1",
                                 {"status": "PASSED", "decided_by": "INSP-1"})
        self.assertEqual(status, 200, body)

    def test_cross_voyage_conclusion_cannot_be_borrowed(self) -> None:
        self._seed_voyage("V1")
        self._register_instrument("I-SAMPLER", "走航采样仪",
                                  "2026-08-01T00:00:00+08:00", "2027-08-01T00:00:00+08:00")
        self._fully_evidence_subject("V1", "SUB-V1", "I-SAMPLER")
        self.post("/subjects/SUB-V1/decision", "inspector", "INSP-1",
                  {"status": "PASSED", "decided_by": "INSP-1"})

        # 第二个航次：新航段、新科目，即便仪器在 V1 已合格也不能借用。
        self._seed_voyage("V2")
        self.post("/voyages/V2/subjects", "coordinator", "COORD-1", {
            "subject_id": "SUB-V2", "code": "SUB-V2", "name": "第二次海试科目",
        })
        self.post("/subjects/SUB-V2/instruments", "coordinator", "COORD-1",
                  {"instrument_id": "I-SAMPLER"})

        # 结构上禁止把 V1 航段的观测挂到 V2 科目。
        status, body = self.post("/subjects/SUB-V2/windows", "researcher", "RES-1", {
            "window_id": "W-STOLEN", "leg_id": "V1-L1",
            "started_at": "2026-09-10T10:00:00+08:00",
            "ended_at": "2026-09-10T12:00:00+08:00",
        })
        self.assertEqual(status, 409)
        self.assertIn("跨航次", body["error"])

        # V2 科目没有自己的证据，判合格被阻止；只能判复测并写原因。
        status, body = self.post("/subjects/SUB-V2/decision", "inspector", "INSP-1",
                                 {"status": "PASSED", "decided_by": "INSP-1"})
        self.assertEqual(status, 422)
        self.assertIn("观测时间窗口", body["error"])

        status, body = self.post("/subjects/SUB-V2/decision", "inspector", "INSP-1",
                                 {"status": "RETEST_REQUIRED", "decided_by": "INSP-1",
                                  "retest_reason": "V2 航次遭遇恶劣天气，采样数据缺失，需补测"})
        self.assertEqual(status, 200, body)

        # 判复测必须给原因。
        self.post("/voyages/V2/subjects", "coordinator", "COORD-1", {
            "subject_id": "SUB-V2B", "code": "SUB-V2B", "name": "另一科目",
        })
        status, body = self.post("/subjects/SUB-V2B/decision", "inspector", "INSP-1",
                                 {"status": "RETEST_REQUIRED", "decided_by": "INSP-1"})
        self.assertEqual(status, 422)
        self.assertIn("复测原因", body["error"])

        # 船东在 V2 航次看到该仪器不具备交付条件，验收视图给出复测原因。
        status, delivery = self.get("/voyages/V2/delivery", "shipowner", "OWNER-1")
        self.assertEqual(status, 200, delivery)
        self.assertFalse(delivery["instruments"][0]["delivery_ready"])
        status, acceptance = self.get("/voyages/V2/acceptance", "inspector", "INSP-1")
        subject = acceptance["instruments"][0]["subjects"][0]
        self.assertEqual(subject["status"], "RETEST_REQUIRED")
        self.assertIn("恶劣天气", subject["retest_reason"])
        self.assertEqual(subject["validated_in_legs"], [])

    def test_expired_calibration_blocks_pass(self) -> None:
        self._seed_voyage("V1")
        self._register_instrument("I-GPS", "定位设备",
                                  "2025-08-01T00:00:00+08:00", "2026-09-01T00:00:00+08:00")
        self._fully_evidence_subject("V1", "SUB-GPS", "I-GPS")
        status, body = self.post("/subjects/SUB-GPS/decision", "inspector", "INSP-1",
                                 {"status": "PASSED", "decided_by": "INSP-1"})
        self.assertEqual(status, 422)
        self.assertIn("校准证书", body["error"])

    def test_role_boundaries(self) -> None:
        # 船东不能登记航次。
        status, body = self.post("/voyages", "shipowner", "OWNER-1", {
            "voyage_id": "VX", "vessel_ref": "V", "title": "x",
            "departure_at": "2026-09-10T08:00:00+08:00",
        })
        self.assertEqual(status, 403)
        # 缺少身份标识拒绝。
        status, _ = self._missing_actor()
        self.assertEqual(status, 401)
        # 研究机构不能下科目结论。
        self._seed_voyage("V1")
        self.post("/voyages/V1/subjects", "coordinator", "COORD-1",
                  {"subject_id": "S1", "code": "S1", "name": "n"})
        status, _ = self.post("/subjects/S1/decision", "researcher", "RES-1",
                              {"status": "PASSED", "decided_by": "RES-1"})
        self.assertEqual(status, 403)
        # 样本授权只能由科研负责人发放。
        self.post("/instruments", "coordinator", "COORD-1", {
            "instrument_id": "I1", "vessel_ref": "VESSEL-DEMO", "name": "n"})
        status, _ = self.post("/samples/NOPE/access-grants", "inspector", "INSP-1",
                              {"grant_id": "g", "institution_ref": "R", "granted_by": "x"})
        self.assertEqual(status, 403)

    def _missing_actor(self) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/voyages/V1/delivery", headers={"X-Actor-Role": "shipowner"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_instrument_must_belong_to_voyage_vessel(self) -> None:
        self._seed_voyage("V1")
        self.post("/instruments", "coordinator", "COORD-1", {
            "instrument_id": "I-OTHER", "vessel_ref": "OTHER-SHIP", "name": "他船仪器"})
        self.post("/voyages/V1/subjects", "coordinator", "COORD-1", {
            "subject_id": "S1", "code": "S1", "name": "n"})
        status, body = self.post("/subjects/S1/instruments", "coordinator", "COORD-1",
                                 {"instrument_id": "I-OTHER"})
        self.assertEqual(status, 409)
        self.assertIn("不属于本科目所在船舶", body["error"])

    def test_health_remains_available(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

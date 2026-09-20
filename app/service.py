"""科研载荷海试验收领域服务。

核心不变量：
1. 科目结论只能由同一航次下挂接的观测窗口与实验记录支撑——
   观测窗口必须引用本航次航段，结构与校验双重禁止跨航次借用结论。
2. 原始读数只增不改；异常读数必须登记复核人，缺失复核不允许判合格。
3. 样本来源仅对获授权研究机构可见；船东只能看到仪器是否满足交付条件。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

ROLE_COORDINATOR = "coordinator"
ROLE_INSPECTOR = "inspector"
ROLE_SHIPOWNER = "shipowner"
ROLE_RESEARCHER = "researcher"


class DomainError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainError(400, f"{field} 必须为 ISO 8601 时间字符串")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(400, f"{field} 时间格式无效：{value}") from exc
    if dt.tzinfo is None:
        raise DomainError(400, f"{field} 必须携带时区偏移，例如 2026-09-10T08:00:00+08:00")
    # 统一存 UTC，保证时间列可按字符串可靠比较。
    return dt.astimezone(timezone.utc)


def _require(payload: dict, field: str) -> Any:
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(400, f"缺少必填字段：{field}")
    return value


def _insert(conn: sqlite3.Connection, sql: str, params: tuple) -> None:
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise DomainError(409, f"记录冲突或引用不存在：{exc}") from exc


# ---------------------------------------------------------------- 记录写入

def create_voyage(conn: sqlite3.Connection, p: dict) -> dict:
    voyage_id = _require(p, "voyage_id")
    vessel_ref = _require(p, "vessel_ref")
    title = _require(p, "title")
    departure_at = _parse_ts(_require(p, "departure_at"), "departure_at")
    returned_at = p.get("returned_at")
    if returned_at is not None:
        _parse_ts(returned_at, "returned_at")
    _insert(
        conn,
        "INSERT INTO voyages(voyage_id, vessel_ref, title, sea_area_ref, departure_at, returned_at, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (voyage_id, vessel_ref, title, p.get("sea_area_ref"),
         departure_at.isoformat(), returned_at, now_iso()),
    )
    return {"voyage_id": voyage_id}


def create_leg(conn: sqlite3.Connection, voyage_id: str, p: dict) -> dict:
    leg_id = _require(p, "leg_id")
    started_at = _parse_ts(_require(p, "started_at"), "started_at")
    ended_at = p.get("ended_at")
    if ended_at is not None:
        ended_dt = _parse_ts(ended_at, "ended_at")
        if ended_dt < started_at:
            raise DomainError(400, "航段结束时间早于开始时间")
        ended_at = ended_dt.isoformat()
    if conn.execute("SELECT 1 FROM voyages WHERE voyage_id=?", (voyage_id,)).fetchone() is None:
        raise DomainError(404, f"航次不存在：{voyage_id}")
    leg_seq = int(p.get("leg_seq") or _next_seq(conn, "voyage_legs", "voyage_id", voyage_id, "leg_seq"))
    _insert(
        conn,
        "INSERT INTO voyage_legs(leg_id, voyage_id, leg_seq, name, started_at, ended_at)"
        " VALUES (?,?,?,?,?,?)",
        (leg_id, voyage_id, leg_seq, _require(p, "name"), started_at.isoformat(), ended_at),
    )
    return {"leg_id": leg_id}


def _next_seq(conn: sqlite3.Connection, table: str, key: str, value: str, seq_col: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX({seq_col}),0)+1 FROM {table} WHERE {key}=?", (value,)).fetchone()
    return int(row[0])


def create_instrument(conn: sqlite3.Connection, p: dict) -> dict:
    instrument_id = _require(p, "instrument_id")
    _insert(
        conn,
        "INSERT INTO instruments(instrument_id, vessel_ref, name, yard_record_ref, installed_at)"
        " VALUES (?,?,?,?,?)",
        (instrument_id, _require(p, "vessel_ref"), _require(p, "name"),
         p.get("yard_record_ref"), p.get("installed_at")),
    )
    return {"instrument_id": instrument_id}


def add_certificate(conn: sqlite3.Connection, instrument_id: str, p: dict) -> dict:
    cert_id = _require(p, "cert_id")
    calibrated_at = _parse_ts(_require(p, "calibrated_at"), "calibrated_at")
    valid_until = _parse_ts(_require(p, "valid_until"), "valid_until")
    if valid_until <= calibrated_at:
        raise DomainError(400, "证书失效时间不得早于校准时间")
    if conn.execute("SELECT 1 FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone() is None:
        raise DomainError(404, f"仪器不存在：{instrument_id}")
    _insert(
        conn,
        "INSERT INTO calibration_certificates(cert_id, instrument_id, cert_ref, issued_by, calibrated_at, valid_until)"
        " VALUES (?,?,?,?,?,?)",
        (cert_id, instrument_id, _require(p, "cert_ref"), p.get("issued_by"),
         calibrated_at.isoformat(), valid_until.isoformat()),
    )
    return {"cert_id": cert_id}


def create_subject(conn: sqlite3.Connection, voyage_id: str, p: dict) -> dict:
    subject_id = _require(p, "subject_id")
    if conn.execute("SELECT 1 FROM voyages WHERE voyage_id=?", (voyage_id,)).fetchone() is None:
        raise DomainError(404, f"航次不存在：{voyage_id}")
    _insert(
        conn,
        "INSERT INTO subjects(subject_id, voyage_id, code, name) VALUES (?,?,?,?)",
        (subject_id, voyage_id, _require(p, "code"), _require(p, "name")),
    )
    return {"subject_id": subject_id}


def link_instrument(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    instrument_id = _require(p, "instrument_id")
    subject = conn.execute("SELECT voyage_id FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
    if subject is None:
        raise DomainError(404, f"科目不存在：{subject_id}")
    instrument = conn.execute("SELECT vessel_ref FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
    if instrument is None:
        raise DomainError(404, f"仪器不存在：{instrument_id}")
    vessel = conn.execute("SELECT vessel_ref FROM voyages WHERE voyage_id=?", (subject["voyage_id"],)).fetchone()
    if vessel["vessel_ref"] != instrument["vessel_ref"]:
        raise DomainError(409, "仪器不属于本科目所在船舶，禁止关联")
    _insert(
        conn,
        "INSERT INTO subject_instruments(subject_id, instrument_id) VALUES (?,?)",
        (subject_id, instrument_id),
    )
    return {"subject_id": subject_id, "instrument_id": instrument_id}


def add_window(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    window_id = _require(p, "window_id")
    leg_id = _require(p, "leg_id")
    started_at = _parse_ts(_require(p, "started_at"), "started_at")
    ended_at = _parse_ts(_require(p, "ended_at"), "ended_at")
    if ended_at <= started_at:
        raise DomainError(400, "观测结束时间必须晚于开始时间")
    subject = conn.execute("SELECT voyage_id FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
    if subject is None:
        raise DomainError(404, f"科目不存在：{subject_id}")
    leg = conn.execute("SELECT voyage_id FROM voyage_legs WHERE leg_id=?", (leg_id,)).fetchone()
    if leg is None:
        raise DomainError(404, f"航段不存在：{leg_id}")
    # 关键约束：观测只能挂在本航次的航段上，结论不可借用其他航次。
    if leg["voyage_id"] != subject["voyage_id"]:
        raise DomainError(409, "航段不属于本科目所在航次，禁止跨航次挂接观测")
    _insert(
        conn,
        "INSERT INTO observation_windows(window_id, subject_id, leg_id, started_at, ended_at, note, recorded_by)"
        " VALUES (?,?,?,?,?,?,?)",
        (window_id, subject_id, leg_id, started_at.isoformat(), ended_at.isoformat(),
         p.get("note"), p.get("recorded_by")),
    )
    return {"window_id": window_id}


def add_run(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    run_id = _require(p, "run_id")
    if conn.execute("SELECT 1 FROM subjects WHERE subject_id=?", (subject_id,)).fetchone() is None:
        raise DomainError(404, f"科目不存在：{subject_id}")
    try:
        params = p.get("params") or {}
        json.dumps(params, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise DomainError(400, "实验参数必须可序列化为 JSON") from exc
    run_seq = int(p.get("run_seq") or _next_seq(conn, "experiment_runs", "subject_id", subject_id, "run_seq"))
    recorded_at = _parse_ts(p.get("recorded_at") or now_iso(), "recorded_at")
    _insert(
        conn,
        "INSERT INTO experiment_runs(run_id, subject_id, run_seq, procedure_ref, params_json,"
        " operator_ref, recorded_at, reproducibility_note)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (run_id, subject_id, run_seq, _require(p, "procedure_ref"),
         json.dumps(params, ensure_ascii=False), p.get("operator_ref"),
         recorded_at.isoformat(), p.get("reproducibility_note")),
    )
    return {"run_id": run_id}


def add_reading(conn: sqlite3.Connection, run_id: str, p: dict) -> dict:
    reading_id = _require(p, "reading_id")
    if conn.execute("SELECT 1 FROM experiment_runs WHERE run_id=?", (run_id,)).fetchone() is None:
        raise DomainError(404, f"实验记录不存在：{run_id}")
    anomaly = 1 if bool(p.get("anomaly")) else 0
    # 异常读数允许在海上先标记、靠港后补登复核人；判合格前仍未复核会被阻断。
    reviewed_by = p.get("reviewed_by")
    reading_at = _parse_ts(_require(p, "reading_at"), "reading_at")
    reviewed_at = now_iso() if (anomaly and reviewed_by) else None
    _insert(
        conn,
        "INSERT INTO readings(reading_id, run_id, metric, raw_value, unit, reading_at,"
        " anomaly, reviewed_by, review_note, reviewed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (reading_id, run_id, _require(p, "metric"), str(_require(p, "raw_value")),
         p.get("unit"), reading_at.isoformat(), anomaly, reviewed_by,
         p.get("review_note") if anomaly else None, reviewed_at),
    )
    return {"reading_id": reading_id, "anomaly": bool(anomaly)}


def review_reading(conn: sqlite3.Connection, reading_id: str, p: dict) -> dict:
    """补登复核信息。原始读数列不在更新语句中，结构上不可篡改。"""
    reviewed_by = _require(p, "reviewed_by")
    row = conn.execute("SELECT anomaly FROM readings WHERE reading_id=?", (reading_id,)).fetchone()
    if row is None:
        raise DomainError(404, f"读数不存在：{reading_id}")
    if not row["anomaly"]:
        raise DomainError(409, "仅异常读数需要复核")
    conn.execute(
        "UPDATE readings SET reviewed_by=?, review_note=?, reviewed_at=? WHERE reading_id=?",
        (reviewed_by, p.get("review_note"), now_iso(), reading_id),
    )
    return {"reading_id": reading_id, "reviewed_by": reviewed_by}


def add_sample(conn: sqlite3.Connection, run_id: str, p: dict) -> dict:
    sample_id = _require(p, "sample_id")
    collected_at = _parse_ts(_require(p, "collected_at"), "collected_at")
    if conn.execute("SELECT 1 FROM experiment_runs WHERE run_id=?", (run_id,)).fetchone() is None:
        raise DomainError(404, f"实验记录不存在：{run_id}")
    _insert(
        conn,
        "INSERT INTO samples(sample_id, sample_ref, run_id, collected_at, specimen_desc, condition_note)"
        " VALUES (?,?,?,?,?,?)",
        (sample_id, _require(p, "sample_ref"), run_id, collected_at.isoformat(),
         p.get("specimen_desc"), p.get("condition_note")),
    )
    return {"sample_id": sample_id}


def add_handover(conn: sqlite3.Connection, sample_id: str, p: dict) -> dict:
    handover_id = _require(p, "handover_id")
    if conn.execute("SELECT 1 FROM samples WHERE sample_id=?", (sample_id,)).fetchone() is None:
        raise DomainError(404, f"样本不存在：{sample_id}")
    handed_at = _parse_ts(_require(p, "handed_at"), "handed_at")
    handover_seq = int(p.get("handover_seq") or _next_seq(conn, "sample_handovers", "sample_id", sample_id, "handover_seq"))
    _insert(
        conn,
        "INSERT INTO sample_handovers(handover_id, sample_id, handover_seq, from_party, to_party,"
        " receiver_ref, handed_at, location, condition_note)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (handover_id, sample_id, handover_seq, _require(p, "from_party"), _require(p, "to_party"),
         _require(p, "receiver_ref"), handed_at.isoformat(), p.get("location"), p.get("condition_note")),
    )
    return {"handover_id": handover_id}


def grant_sample_access(conn: sqlite3.Connection, sample_id: str, p: dict) -> dict:
    grant_id = _require(p, "grant_id")
    if conn.execute("SELECT 1 FROM samples WHERE sample_id=?", (sample_id,)).fetchone() is None:
        raise DomainError(404, f"样本不存在：{sample_id}")
    _insert(
        conn,
        "INSERT INTO sample_access_grants(grant_id, sample_id, institution_ref, scope_note, granted_by, granted_at)"
        " VALUES (?,?,?,?,?,?)",
        (grant_id, sample_id, _require(p, "institution_ref"), p.get("scope_note"),
         _require(p, "granted_by"), now_iso()),
    )
    return {"grant_id": grant_id}


# ---------------------------------------------------------------- 科目判定

def _calibration_covers(conn: sqlite3.Connection, instrument_id: str,
                        start_iso: str, end_iso: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM calibration_certificates WHERE instrument_id=?"
        " AND calibrated_at <= ? AND valid_until >= ? LIMIT 1",
        (instrument_id, start_iso, end_iso),
    ).fetchone()
    return row is not None


def decide_subject(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    status = _require(p, "status")
    if status not in ("PASSED", "RETEST_REQUIRED"):
        raise DomainError(400, "status 只能为 PASSED 或 RETEST_REQUIRED")
    decided_by = _require(p, "decided_by")
    subject = conn.execute(
        "SELECT voyage_id, status FROM subjects WHERE subject_id=?", (subject_id,)
    ).fetchone()
    if subject is None:
        raise DomainError(404, f"科目不存在：{subject_id}")

    retest_reason = p.get("retest_reason")
    if status == "RETEST_REQUIRED" and not retest_reason:
        raise DomainError(422, "判定需复测必须填写复测原因")

    blockers: list[str] = []
    if status == "PASSED":
        windows = conn.execute(
            "SELECT window_id, started_at, ended_at FROM observation_windows WHERE subject_id=?",
            (subject_id,),
        ).fetchall()
        runs = conn.execute(
            "SELECT run_id FROM experiment_runs WHERE subject_id=?", (subject_id,)
        ).fetchall()
        if not windows:
            blockers.append("缺少本航次海上观测时间窗口")
        if not runs:
            blockers.append("缺少可复现实验记录")
        reading_count = conn.execute(
            "SELECT COUNT(*) AS n FROM readings r JOIN experiment_runs e ON r.run_id=e.run_id"
            " WHERE e.subject_id=?", (subject_id,),
        ).fetchone()["n"]
        if reading_count == 0:
            blockers.append("缺少任何观测读数")
        unreviewed = conn.execute(
            "SELECT COUNT(*) AS n FROM readings r JOIN experiment_runs e ON r.run_id=e.run_id"
            " WHERE e.subject_id=? AND r.anomaly=1 AND r.reviewed_by IS NULL",
            (subject_id,),
        ).fetchone()["n"]
        if unreviewed:
            blockers.append(f"存在 {unreviewed} 条未经复核的异常读数")
        instruments = conn.execute(
            "SELECT i.instrument_id, i.name FROM subject_instruments si"
            " JOIN instruments i ON i.instrument_id=si.instrument_id WHERE si.subject_id=?",
            (subject_id,),
        ).fetchall()
        if not instruments:
            blockers.append("科目未关联任何装船仪器")
        for inst in instruments:
            for w in windows:
                if not _calibration_covers(conn, inst["instrument_id"], w["started_at"], w["ended_at"]):
                    blockers.append(
                        f"仪器 {inst['name']} 在观测窗口 {w['window_id']} 期间无有效校准证书"
                    )
        if blockers:
            raise DomainError(422, "科目不具备合格条件：" + "；".join(blockers))

    conn.execute(
        "UPDATE subjects SET status=?, retest_reason=?, decided_by=?, decided_at=? WHERE subject_id=?",
        (status, retest_reason if status == "RETEST_REQUIRED" else None,
         decided_by, now_iso(), subject_id),
    )
    return {"subject_id": subject_id, "status": status, "blockers": blockers}


# ---------------------------------------------------------------- 视图

def _load_voyage_or_404(conn: sqlite3.Connection, voyage_id: str) -> sqlite3.Row:
    voyage = conn.execute("SELECT * FROM voyages WHERE voyage_id=?", (voyage_id,)).fetchone()
    if voyage is None:
        raise DomainError(404, f"航次不存在：{voyage_id}")
    return voyage


def delivery_report(conn: sqlite3.Connection, voyage_id: str) -> dict:
    """船东视图：仅给出每件仪器在本航次是否满足交付条件，不暴露研究数据。"""
    voyage = _load_voyage_or_404(conn, voyage_id)
    rows = conn.execute(
        "SELECT i.instrument_id, i.name, si.subject_id, s.code, s.status, s.retest_reason"
        " FROM instruments i"
        " JOIN subject_instruments si ON si.instrument_id=i.instrument_id"
        " JOIN subjects s ON s.subject_id=si.subject_id"
        " WHERE s.voyage_id=? AND i.vessel_ref=?",
        (voyage_id, voyage["vessel_ref"]),
    ).fetchall()
    instruments: dict[str, dict] = {}
    for r in rows:
        item = instruments.setdefault(r["instrument_id"], {
            "instrument_id": r["instrument_id"],
            "name": r["name"],
            "subjects": [],
            "delivery_ready": True,
        })
        item["subjects"].append({
            "subject_code": r["code"],
            "status": r["status"],
            "retest_reason": r["retest_reason"],
        })
        if r["status"] != "PASSED":
            item["delivery_ready"] = False
    # 关联了本科目航次但没有任何科目结论的仪器也不具备交付条件。
    for item in instruments.values():
        if any(s["status"] == "PENDING" for s in item["subjects"]):
            item["delivery_ready"] = False
    return {
        "voyage_id": voyage_id,
        "vessel_ref": voyage["vessel_ref"],
        "instruments": sorted(instruments.values(), key=lambda x: x["instrument_id"]),
    }


def acceptance_report(conn: sqlite3.Connection, voyage_id: str) -> dict:
    """验收人员视图：每件仪器在哪段航行（航段）完成验证、结论与复测原因。"""
    _load_voyage_or_404(conn, voyage_id)
    rows = conn.execute(
        "SELECT i.instrument_id, i.name AS instrument_name, s.subject_id, s.code AS subject_code,"
        " s.name AS subject_name, s.status, s.retest_reason, s.decided_by, s.decided_at,"
        " l.leg_id, l.name AS leg_name, l.leg_seq, w.started_at, w.ended_at"
        " FROM subject_instruments si"
        " JOIN instruments i ON i.instrument_id=si.instrument_id"
        " JOIN subjects s ON s.subject_id=si.subject_id"
        " LEFT JOIN observation_windows w ON w.subject_id=s.subject_id"
        " LEFT JOIN voyage_legs l ON l.leg_id=w.leg_id"
        " WHERE s.voyage_id=?"
        " ORDER BY i.instrument_id, s.code, l.leg_seq, w.started_at",
        (voyage_id,),
    ).fetchall()
    instruments: dict[str, dict] = {}
    for r in rows:
        item = instruments.setdefault(r["instrument_id"], {
            "instrument_id": r["instrument_id"],
            "name": r["instrument_name"],
            "subjects": {},
        })
        subject = item["subjects"].setdefault(r["subject_id"], {
            "subject_id": r["subject_id"],
            "code": r["subject_code"],
            "name": r["subject_name"],
            "status": r["status"],
            "retest_reason": r["retest_reason"],
            "decided_by": r["decided_by"],
            "decided_at": r["decided_at"],
            "validated_in_legs": [],
        })
        if r["leg_id"] is not None:
            subject["validated_in_legs"].append({
                "leg_id": r["leg_id"],
                "leg_seq": r["leg_seq"],
                "name": r["leg_name"],
                "window_started_at": r["started_at"],
                "window_ended_at": r["ended_at"],
            })
    result = []
    for item in instruments.values():
        item["subjects"] = sorted(item["subjects"].values(), key=lambda s: s["code"])
        result.append(item)
    return {"voyage_id": voyage_id, "instruments": result}


def sample_provenance(conn: sqlite3.Connection, sample_ref: str, institution_ref: str) -> dict:
    """研究机构视图：仅可追溯被授权样本的来源、实验复现要素与冷藏交接链。"""
    sample = conn.execute(
        "SELECT s.sample_id, s.sample_ref, s.collected_at, s.specimen_desc, s.condition_note,"
        " e.run_id, e.procedure_ref, e.params_json, e.operator_ref, e.recorded_at,"
        " e.reproducibility_note, sub.subject_id, sub.code AS subject_code, sub.voyage_id"
        " FROM samples s"
        " JOIN experiment_runs e ON e.run_id=s.run_id"
        " JOIN subjects sub ON sub.subject_id=e.subject_id"
        " WHERE s.sample_ref=?",
        (sample_ref,),
    ).fetchone()
    if sample is None:
        raise DomainError(404, f"样本不存在：{sample_ref}")
    grant = conn.execute(
        "SELECT scope_note FROM sample_access_grants WHERE sample_id=? AND institution_ref=?",
        (sample["sample_id"], institution_ref),
    ).fetchone()
    if grant is None:
        # 不暴露样本是否存在之外的信息，统一按禁止访问处理。
        raise DomainError(403, "研究机构未获得该样本的追溯授权")
    handovers = conn.execute(
        "SELECT handover_seq, from_party, to_party, receiver_ref, handed_at, location, condition_note"
        " FROM sample_handovers WHERE sample_id=? ORDER BY handover_seq",
        (sample["sample_id"],),
    ).fetchall()
    return {
        "sample_ref": sample["sample_ref"],
        "collected_at": sample["collected_at"],
        "specimen_desc": sample["specimen_desc"],
        "condition_note": sample["condition_note"],
        "authorization_scope": grant["scope_note"],
        "source": {
            "voyage_id": sample["voyage_id"],
            "subject_code": sample["subject_code"],
            "run_id": sample["run_id"],
            "procedure_ref": sample["procedure_ref"],
            "params": json.loads(sample["params_json"]),
            "operator_ref": sample["operator_ref"],
            "recorded_at": sample["recorded_at"],
            "reproducibility_note": sample["reproducibility_note"],
        },
        "handovers": [dict(h) for h in handovers],
    }


def sample_readings(conn: sqlite3.Connection, sample_ref: str, institution_ref: str) -> dict:
    """附带原始读数（含异常标记与复核人），仍受同一授权约束。"""
    base = sample_provenance(conn, sample_ref, institution_ref)
    sample = conn.execute("SELECT sample_id, run_id FROM samples WHERE sample_ref=?", (sample_ref,)).fetchone()
    readings = conn.execute(
        "SELECT metric, raw_value, unit, reading_at, anomaly, reviewed_by, review_note, reviewed_at"
        " FROM readings WHERE run_id=? ORDER BY reading_at",
        (sample["run_id"],),
    ).fetchall()
    base["readings"] = [
        {**dict(r), "anomaly": bool(r["anomaly"])} for r in readings
    ]
    return base


# ---------------------------------------------------------------- 鉴权

def require_role(headers: dict[str, str], allowed: set[str]) -> tuple[str, str]:
    role = headers.get("x-actor-role", "").strip().lower()
    actor = headers.get("x-actor-ref", "").strip()
    if role not in allowed:
        raise DomainError(403, f"当前角色无权执行该操作，允许角色：{sorted(allowed)}")
    if not actor:
        raise DomainError(401, "缺少 X-Actor-Ref 身份标识")
    return role, actor

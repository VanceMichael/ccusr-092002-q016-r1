
"""科研载荷海试验收领域服务。

关键不变量：
1. 科目的合格结论只取本航次证据（证书、观测窗、实验、复核均按航次过滤），
   其他航次的"通过"不能冒用。
2. 原始读数只追加、不可改删；异常读数必须登记复核人后，科目才算完成。
3. 样本来源只对授权研究机构开放，授权按样本或科目界定范围。
"""

import json
import sqlite3
from datetime import datetime


class DomainError(Exception):
    def __init__(self, status: int, message: str, details: object = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


# ---------- 基础校验 ----------

def require(payload: dict, fields: list[str]) -> None:
    missing = [f for f in fields if payload.get(f) in (None, "")]
    if missing:
        raise DomainError(400, "缺少必填字段", missing)


def parse_time(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainError(400, f"时间字段 {field} 必须为 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise DomainError(400, f"时间字段 {field} 必须带时区偏移（ISO 8601 with offset）")
    return parsed


def _json_object(value: str | dict | None, field: str) -> str:
    if value is None:
        return "{}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise DomainError(400, f"字段 {field} 必须是 JSON 对象")
    if not isinstance(parsed, dict):
        raise DomainError(400, f"字段 {field} 必须是 JSON 对象")
    return value if isinstance(value, str) else json.dumps(parsed, ensure_ascii=False)


def _get(conn: sqlite3.Connection, table: str, key: str, value: object) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (value,)).fetchone()
    if row is None:
        raise DomainError(404, f"{table} 中不存在 {key}={value}")
    return row


# ---------- 录入：航次 / 航段 / 仪器 ----------

def create_voyage(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "vessel_ref", "departure_at"])
    parse_time(p["departure_at"], "departure_at")
    if p.get("arrived_at"):
        parse_time(p["arrived_at"], "arrived_at")
    try:
        conn.execute(
            "INSERT INTO voyages(id, vessel_ref, sea_area_ref, departure_at, arrived_at, note)"
            " VALUES (?,?,?,?,?,?)",
            (p["id"], p["vessel_ref"], p.get("sea_area_ref"), p["departure_at"],
             p.get("arrived_at"), p.get("note")),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"航次 {p['id']} 已存在")
    return {"id": p["id"]}


def add_leg(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["voyage_id", "started_at"])
    parse_time(p["started_at"], "started_at")
    if p.get("ended_at"):
        parse_time(p["ended_at"], "ended_at")
    _get(conn, "voyages", "voyage_id", p["voyage_id"])
    seq = p.get("seq")
    if seq is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM legs WHERE voyage_id = ?",
            (p["voyage_id"],),
        ).fetchone()
        seq = row["seq"]
    try:
        cur = conn.execute(
            "INSERT INTO legs(voyage_id, seq, started_at, ended_at, phase)"
            " VALUES (?,?,?,?,?)",
            (p["voyage_id"], seq, p["started_at"], p.get("ended_at"),
             p.get("phase", "海试")),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"航次 {p['voyage_id']} 中航段序号 {seq} 已存在")
    return {"id": cur.lastrowid, "voyage_id": p["voyage_id"], "seq": seq}


def create_instrument(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "name", "category"])
    try:
        conn.execute(
            "INSERT INTO instruments(id, name, category, installed_voyage_id)"
            " VALUES (?,?,?,?)",
            (p["id"], p["name"], p["category"], p.get("installed_voyage_id")),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"仪器 {p['id']} 已存在")
    return {"id": p["id"]}


def add_certificate(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "instrument_id", "voyage_id", "issuer", "calibrated_at"])
    parse_time(p["calibrated_at"], "calibrated_at")
    if p.get("valid_until"):
        parse_time(p["valid_until"], "valid_until")
    _get(conn, "instruments", "instrument_id", p["instrument_id"])
    voyage = _get(conn, "voyages", "voyage_id", p["voyage_id"])
    result = p.get("result", "合格")
    if result not in ("合格", "限用", "停用"):
        raise DomainError(400, "证书结论只能为 合格/限用/停用")
    # 证书必须在开航前仍有效（valid_until 为空视为长期有效）
    if p.get("valid_until"):
        if parse_time(p["valid_until"], "valid_until") < parse_time(voyage["departure_at"], "departure_at"):
            raise DomainError(422, "校准证书在该航次开航前已失效", [p["id"]])
    try:
        conn.execute(
            "INSERT INTO calibration_certificates"
            "(id, instrument_id, voyage_id, issuer, calibrated_at, valid_until, result)"
            " VALUES (?,?,?,?,?,?,?)",
            (p["id"], p["instrument_id"], p["voyage_id"], p["issuer"],
             p["calibrated_at"], p.get("valid_until"), result),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"证书 {p['id']} 已存在")
    return {"id": p["id"]}


# ---------- 科目与关联仪器 ----------

def create_subject(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "voyage_id", "name", "instrument_ids"])
    instrument_ids = p["instrument_ids"]
    if not isinstance(instrument_ids, list) or not instrument_ids:
        raise DomainError(400, "instrument_ids 必须是非空数组")
    _get(conn, "voyages", "voyage_id", p["voyage_id"])
    for instrument_id in instrument_ids:
        _get(conn, "instruments", "instrument_id", instrument_id)
    try:
        conn.execute(
            "INSERT INTO subjects(id, voyage_id, name, requirement, status)"
            " VALUES (?,?,?,?, '进行中')",
            (p["id"], p["voyage_id"], p["name"], p.get("requirement")),
        )
        conn.executemany(
            "INSERT INTO subject_instruments(subject_id, instrument_id) VALUES (?,?)",
            [(p["id"], iid) for iid in instrument_ids],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"科目 {p['id']} 已存在或仪器关联冲突")
    return {"id": p["id"], "voyage_id": p["voyage_id"]}


def _subject_instruments(conn: sqlite3.Connection, subject_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT i.* FROM instruments i"
        " JOIN subject_instruments si ON si.instrument_id = i.id"
        " WHERE si.subject_id = ? ORDER BY i.id",
        (subject_id,),
    ).fetchall()


def _ensure_leg_in_voyage(conn: sqlite3.Connection, leg_id: int, voyage_id: str) -> sqlite3.Row:
    leg = _get(conn, "legs", "leg_id", leg_id)
    if leg["voyage_id"] != voyage_id:
        raise DomainError(422, "航段不属于该科目所在航次，禁止跨航次挂接证据",
                          [f"leg_id={leg_id}", f"voyage_id={voyage_id}"])
    return leg


def _ensure_instrument_in_subject(conn: sqlite3.Connection, subject_id: str, instrument_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM subject_instruments WHERE subject_id = ? AND instrument_id = ?",
        (subject_id, instrument_id),
    ).fetchone()
    if row is None:
        raise DomainError(422, "仪器未关联到该科目", [subject_id, instrument_id])


# ---------- 观测时间 / 实验记录 / 读数 ----------

def add_observation(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["subject_id", "leg_id", "instrument_id", "started_at", "observer"])
    parse_time(p["started_at"], "started_at")
    if p.get("ended_at"):
        parse_time(p["ended_at"], "ended_at")
    subject = _get(conn, "subjects", "subject_id", p["subject_id"])
    _ensure_leg_in_voyage(conn, p["leg_id"], subject["voyage_id"])
    _ensure_instrument_in_subject(conn, p["subject_id"], p["instrument_id"])
    cur = conn.execute(
        "INSERT INTO observation_windows"
        "(subject_id, leg_id, instrument_id, started_at, ended_at, observer)"
        " VALUES (?,?,?,?,?,?)",
        (p["subject_id"], p["leg_id"], p["instrument_id"], p["started_at"],
         p.get("ended_at"), p["observer"]),
    )
    conn.commit()
    return {"id": cur.lastrowid}


def add_experiment(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "subject_id", "leg_id", "instrument_id", "protocol_ref", "operator", "ran_at"])
    parse_time(p["ran_at"], "ran_at")
    subject = _get(conn, "subjects", "subject_id", p["subject_id"])
    _ensure_leg_in_voyage(conn, p["leg_id"], subject["voyage_id"])
    _ensure_instrument_in_subject(conn, p["subject_id"], p["instrument_id"])
    parameters = _json_object(p.get("parameters"), "parameters")
    try:
        conn.execute(
            "INSERT INTO experiment_runs"
            "(id, subject_id, leg_id, instrument_id, protocol_ref, parameters, operator, ran_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (p["id"], p["subject_id"], p["leg_id"], p["instrument_id"],
             p["protocol_ref"], parameters, p["operator"], p["ran_at"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"实验记录 {p['id']} 已存在")
    return {"id": p["id"]}


def add_readings(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["experiment_run_id", "readings"])
    run = _get(conn, "experiment_runs", "experiment_run_id", p["experiment_run_id"])
    items = p["readings"]
    if not isinstance(items, list) or not items:
        raise DomainError(400, "readings 必须是非空数组")
    ids = []
    for item in items:
        require(item, ["metric", "raw_value", "recorded_at"])
        parse_time(item["recorded_at"], "recorded_at")
        is_anomaly = 1 if item.get("is_anomaly") else 0
        # 异常读数登记时即可附复核意见，也可稍后通过复核接口补登
        reviewed_by = item.get("reviewed_by")
        review_result = item.get("review_result")
        anomaly_reason = item.get("anomaly_reason")
        if is_anomaly and ((reviewed_by is None) != (review_result is None)):
            raise DomainError(400, "异常读数的复核人与复核结论必须同时提供")
        if not is_anomaly and (reviewed_by or review_result or anomaly_reason):
            raise DomainError(400, "只有异常读数才能登记异常原因与复核信息")
        if review_result and review_result not in ("确认异常", "误报修正"):
            raise DomainError(400, "review_result 只能为 确认异常/误报修正")
        cur = conn.execute(
            "INSERT INTO readings"
            "(experiment_run_id, metric, raw_value, unit, recorded_at, is_anomaly,"
            " anomaly_reason, reviewed_by, reviewed_at, review_result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run["id"], item["metric"], str(item["raw_value"]), item.get("unit"),
             item["recorded_at"], is_anomaly, anomaly_reason, reviewed_by,
             item.get("reviewed_at"), review_result),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return {"ids": ids}


def review_reading(conn: sqlite3.Connection, reading_id: int, p: dict) -> dict:
    """复核异常读数。复核信息一旦登记不可更改，原始读数始终保留。"""
    require(p, ["reviewed_by", "review_result"])
    if p["review_result"] not in ("确认异常", "误报修正"):
        raise DomainError(400, "review_result 只能为 确认异常/误报修正")
    row = conn.execute("SELECT * FROM readings WHERE id = ?", (reading_id,)).fetchone()
    if row is None:
        raise DomainError(404, f"读数 {reading_id} 不存在")
    if not row["is_anomaly"]:
        raise DomainError(422, "只有异常读数需要复核")
    if row["reviewed_by"] is not None:
        raise DomainError(409, "该读数已完成复核，复核记录不可更改",
                          [row["reviewed_by"], row["review_result"]])
    reviewed_at = p.get("reviewed_at")
    if reviewed_at:
        parse_time(reviewed_at, "reviewed_at")
    conn.execute(
        "UPDATE readings SET reviewed_by = ?, reviewed_at = COALESCE(?, CURRENT_TIMESTAMP),"
        " review_result = ? WHERE id = ?",
        (p["reviewed_by"], reviewed_at, p["review_result"], reading_id),
    )
    conn.commit()
    return {"id": reading_id, "reviewed_by": p["reviewed_by"], "review_result": p["review_result"]}


# ---------- 样本与交接链 ----------

def add_sample(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["id", "subject_id", "storage_instrument_id", "collected_at", "collected_by"])
    parse_time(p["collected_at"], "collected_at")
    subject = _get(conn, "subjects", "subject_id", p["subject_id"])
    _get(conn, "instruments", "instrument_id", p["storage_instrument_id"])
    _ensure_instrument_in_subject(conn, p["subject_id"], p["storage_instrument_id"])
    if p.get("experiment_run_id"):
        run = _get(conn, "experiment_runs", "experiment_run_id", p["experiment_run_id"])
        if run["subject_id"] != p["subject_id"]:
            raise DomainError(422, "实验记录不属于该科目")
    metadata = _json_object(p.get("metadata_json"), "metadata_json")
    try:
        conn.execute(
            "INSERT INTO samples"
            "(id, subject_id, experiment_run_id, storage_instrument_id,"
            " collected_at, collected_by, metadata_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (p["id"], p["subject_id"], p.get("experiment_run_id"),
             p["storage_instrument_id"], p["collected_at"], p["collected_by"], metadata),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise DomainError(409, f"样本 {p['id']} 已存在")
    return {"id": p["id"]}


def add_handover(conn: sqlite3.Connection, sample_id: str, p: dict) -> dict:
    require(p, ["from_party", "to_party", "handed_at"])
    parse_time(p["handed_at"], "handed_at")
    _get(conn, "samples", "sample_id", sample_id)
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM sample_handovers WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    seq = row["seq"]
    conn.execute(
        "INSERT INTO sample_handovers(sample_id, seq, from_party, to_party, handed_at, condition)"
        " VALUES (?,?,?,?,?,?)",
        (sample_id, seq, p["from_party"], p["to_party"], p["handed_at"],
         p.get("condition", "完好")),
    )
    conn.commit()
    return {"sample_id": sample_id, "seq": seq}


# ---------- 授权 ----------

def grant_sample_access(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["org_ref", "granted_by"])
    if not p.get("sample_id") and not p.get("subject_id"):
        raise DomainError(400, "必须指定 sample_id 或 subject_id 作为授权范围")
    if p.get("sample_id"):
        _get(conn, "samples", "sample_id", p["sample_id"])
    if p.get("subject_id"):
        _get(conn, "subjects", "subject_id", p["subject_id"])
    granted_at = p.get("granted_at")
    if granted_at:
        parse_time(granted_at, "granted_at")
    cur = conn.execute(
        "INSERT INTO sample_grants(org_ref, sample_id, subject_id, granted_by, granted_at)"
        " VALUES (?,?,?,?,COALESCE(?, CURRENT_TIMESTAMP))",
        (p["org_ref"], p.get("sample_id"), p.get("subject_id"), p["granted_by"], granted_at),
    )
    conn.commit()
    return {"id": cur.lastrowid, "org_ref": p["org_ref"],
            "sample_id": p.get("sample_id"), "subject_id": p.get("subject_id")}


# ---------- 科目完成判定（核心不变量） ----------

def _subject_unresolved_anomalies(conn: sqlite3.Connection, subject_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM readings r"
        " JOIN experiment_runs e ON e.id = r.experiment_run_id"
        " WHERE e.subject_id = ? AND r.is_anomaly = 1 AND r.reviewed_by IS NULL",
        (subject_id,),
    ).fetchone()["n"]


def evaluate_subject(conn: sqlite3.Connection, subject: sqlite3.Row) -> dict:
    """严格按本科目所在航次汇总证据，其他航次记录一律不计入。"""
    voyage_id = subject["voyage_id"]
    instruments = _subject_instruments(conn, subject["id"])
    checks: list[dict] = []
    blockers: list[str] = []

    for inst in instruments:
        # 1) 本航次有效校准证书
        cert = conn.execute(
            "SELECT * FROM calibration_certificates"
            " WHERE instrument_id = ? AND voyage_id = ? AND result = '合格'"
            " ORDER BY calibrated_at DESC LIMIT 1",
            (inst["id"], voyage_id),
        ).fetchone()
        cert_ok = cert is not None
        if not cert_ok:
            blockers.append(f"仪器 {inst['id']}（{inst['name']}）缺少本航次合格校准证书")

        # 2) 本航次海上观测时间窗（经由航段归属航次二次约束）
        obs = conn.execute(
            "SELECT COUNT(*) AS n FROM observation_windows o"
            " JOIN legs l ON l.id = o.leg_id"
            " WHERE o.subject_id = ? AND o.instrument_id = ? AND l.voyage_id = ?",
            (subject["id"], inst["id"], voyage_id),
        ).fetchone()["n"]
        obs_ok = obs > 0
        if not obs_ok:
            blockers.append(f"仪器 {inst['id']}（{inst['name']}）缺少本航次海上观测时间记录")

        # 3) 可复现实验记录
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM experiment_runs e"
            " JOIN legs l ON l.id = e.leg_id"
            " WHERE e.subject_id = ? AND e.instrument_id = ? AND l.voyage_id = ?",
            (subject["id"], inst["id"], voyage_id),
        ).fetchone()["n"]
        runs_ok = runs > 0
        if not runs_ok:
            blockers.append(f"仪器 {inst['id']}（{inst['name']}）缺少可复现实验记录")

        checks.append({"instrument_id": inst["id"], "instrument_name": inst["name"],
                       "qualified_certificate": cert_ok,
                       "observation_windows": obs, "experiment_runs": runs,
                       "passed": cert_ok and obs_ok and runs_ok})

    # 4) 异常读数均已复核并登记复核人
    unresolved = _subject_unresolved_anomalies(conn, subject["id"])
    anomalies_ok = unresolved == 0
    if not anomalies_ok:
        blockers.append(f"存在 {unresolved} 条异常读数尚未登记复核人")

    evidence_passed = all(c["passed"] for c in checks) and anomalies_ok
    if subject["status"] == "未通过":
        computed = "不通过"
    elif evidence_passed:
        computed = "通过"
    else:
        computed = "待复测"
    return {"subject_id": subject["id"], "name": subject["name"],
            "voyage_id": voyage_id, "status": subject["status"],
            "computed_result": computed, "instrument_checks": checks,
            "unresolved_anomalies": unresolved, "blockers": blockers}


def complete_subject(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    require(p, ["completed_by"])
    subject = _get(conn, "subjects", "subject_id", subject_id)
    evaluation = evaluate_subject(conn, subject)
    if not evaluation["blockers"] and subject["status"] != "未通过":
        conn.execute("UPDATE subjects SET status = '已完成' WHERE id = ?", (subject_id,))
        conn.commit()
        evaluation["status"] = "已完成"
        return evaluation
    raise DomainError(422, "科目证据不完整，不能标记为已完成；请按 blockers 补测或复核",
                      evaluation["blockers"])


def fail_subject(conn: sqlite3.Connection, subject_id: str, p: dict) -> dict:
    require(p, ["reason"])
    _get(conn, "subjects", "subject_id", subject_id)
    conn.execute("UPDATE subjects SET status = '未通过' WHERE id = ?", (subject_id,))
    conn.commit()
    return {"subject_id": subject_id, "status": "未通过", "reason": p["reason"]}


# ---------- 验收记录与报告 ----------

def record_validation(conn: sqlite3.Connection, p: dict) -> dict:
    require(p, ["instrument_id", "voyage_id", "result", "decided_by", "decided_at"])
    parse_time(p["decided_at"], "decided_at")
    if p["result"] not in ("通过", "待复测", "不通过"):
        raise DomainError(400, "验收结论只能为 通过/待复测/不通过")
    if p["result"] != "通过" and not p.get("reason"):
        raise DomainError(400, "待复测/不通过必须填写原因")
    _get(conn, "instruments", "instrument_id", p["instrument_id"])
    _get(conn, "voyages", "voyage_id", p["voyage_id"])
    leg_id = p.get("leg_id")
    if leg_id is not None:
        _ensure_leg_in_voyage(conn, leg_id, p["voyage_id"])
    linked = conn.execute(
        "SELECT 1 FROM subject_instruments si JOIN subjects s ON s.id = si.subject_id"
        " WHERE si.instrument_id = ? AND s.voyage_id = ? LIMIT 1",
        (p["instrument_id"], p["voyage_id"]),
    ).fetchone()
    if linked is None:
        raise DomainError(422, "该仪器在本航次没有关联任何试航科目，无法验收")
    conn.execute(
        "INSERT INTO instrument_validations"
        "(instrument_id, voyage_id, leg_id, result, reason, decided_by, decided_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(instrument_id, voyage_id) DO UPDATE SET"
        " leg_id = excluded.leg_id, result = excluded.result, reason = excluded.reason,"
        " decided_by = excluded.decided_by, decided_at = excluded.decided_at",
        (p["instrument_id"], p["voyage_id"], leg_id, p["result"],
         p.get("reason"), p["decided_by"], p["decided_at"]),
    )
    conn.commit()
    return {"instrument_id": p["instrument_id"], "voyage_id": p["voyage_id"],
            "result": p["result"], "reason": p.get("reason")}


def _verified_legs(conn: sqlite3.Connection, voyage_id: str, instrument_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT l.id, l.seq, l.phase, l.started_at, l.ended_at FROM legs l"
        " JOIN observation_windows o ON o.leg_id = l.id"
        " JOIN subjects s ON s.id = o.subject_id"
        " WHERE l.voyage_id = ? AND o.instrument_id = ? AND s.voyage_id = ?"
        " ORDER BY l.seq",
        (voyage_id, instrument_id, voyage_id),
    ).fetchall()
    return [dict(r) for r in rows]


def voyage_report(conn: sqlite3.Connection, voyage_id: str) -> dict:
    """靠港后验收报告：每项仪器在哪段航段验证、结论与复测原因。"""
    _get(conn, "voyages", "voyage_id", voyage_id)
    subjects = conn.execute(
        "SELECT * FROM subjects WHERE voyage_id = ? ORDER BY id", (voyage_id,)
    ).fetchall()
    subject_reports = [evaluate_subject(conn, s) for s in subjects]

    instrument_ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT i.id FROM instruments i"
        " JOIN subject_instruments si ON si.instrument_id = i.id"
        " JOIN subjects s ON s.id = si.subject_id"
        " WHERE s.voyage_id = ? ORDER BY i.id", (voyage_id,)).fetchall()]
    instruments = []
    for iid in instrument_ids:
        inst = conn.execute("SELECT * FROM instruments WHERE id = ?", (iid,)).fetchone()
        blockers: list[str] = []
        for sr in subject_reports:
            if any(c["instrument_id"] == iid for c in sr["instrument_checks"]):
                blockers.extend(
                    [f"[{sr['name']}] {b}" for b in sr["blockers"]
                     if iid in b or sr["name"] in b]
                )
        if any(sr["status"] == "未通过" and
               any(c["instrument_id"] == iid for c in sr["instrument_checks"])
               for sr in subject_reports):
            computed = "不通过"
        elif blockers:
            computed = "待复测"
        else:
            computed = "通过"
        decision = conn.execute(
            "SELECT * FROM instrument_validations WHERE instrument_id = ? AND voyage_id = ?",
            (iid, voyage_id),
        ).fetchone()
        instruments.append({
            "instrument_id": iid, "name": inst["name"], "category": inst["category"],
            "computed_result": computed,
            "verified_in_legs": _verified_legs(conn, voyage_id, iid),
            "blocking_reasons": blockers,
            "acceptance_decision": None if decision is None else {
                "result": decision["result"], "reason": decision["reason"],
                "leg_id": decision["leg_id"], "decided_by": decision["decided_by"],
                "decided_at": decision["decided_at"],
            },
        })
    return {"voyage_id": voyage_id, "subjects": subject_reports, "instruments": instruments}


# ---------- 角色视图 ----------

def owner_delivery(conn: sqlite3.Connection, voyage_id: str) -> dict:
    """船东视图：只返回设备是否满足交付条件，不含证据明细与人员信息。"""
    _get(conn, "voyages", "voyage_id", voyage_id)
    report = voyage_report(conn, voyage_id)
    items = []
    for inst in report["instruments"]:
        decision = inst["acceptance_decision"]
        meets = inst["computed_result"] == "通过" and decision is not None and decision["result"] == "通过"
        items.append({"instrument_id": inst["instrument_id"], "name": inst["name"],
                      "category": inst["category"], "meets_delivery_conditions": meets,
                      "delivery_result": "满足交付" if meets else "暂不满足交付"})
    return {"voyage_id": voyage_id, "instruments": items}


def _granted_sample_ids(conn: sqlite3.Connection, org_ref: str) -> set[str]:
    rows = conn.execute(
        "SELECT sample_id FROM sample_grants WHERE org_ref = ? AND sample_id IS NOT NULL",
        (org_ref,),
    ).fetchall()
    ids = {r["sample_id"] for r in rows}
    subject_rows = conn.execute(
        "SELECT subject_id FROM sample_grants WHERE org_ref = ? AND subject_id IS NOT NULL",
        (org_ref,),
    ).fetchall()
    for r in subject_rows:
        for s in conn.execute("SELECT id FROM samples WHERE subject_id = ?", (r["subject_id"],)):
            ids.add(s["id"])
    return ids


def researcher_samples(conn: sqlite3.Connection, org_ref: str) -> dict:
    """研究机构视图：仅返回授权范围内样本的来源与交接链。"""
    allowed = _granted_sample_ids(conn, org_ref)
    samples = []
    for sid in sorted(allowed):
        row = conn.execute(
            "SELECT s.*, sub.voyage_id, sub.name AS subject_name FROM samples s"
            " JOIN subjects sub ON sub.id = s.subject_id WHERE s.id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            continue
        source = {
            "sample_id": row["id"], "subject_id": row["subject_id"],
            "subject_name": row["subject_name"], "voyage_id": row["voyage_id"],
            "collected_at": row["collected_at"], "collected_by": row["collected_by"],
            "storage_instrument_id": row["storage_instrument_id"],
            "metadata": json.loads(row["metadata_json"]),
        }
        if row["experiment_run_id"]:
            run = conn.execute(
                "SELECT protocol_ref, parameters, ran_at, leg_id FROM experiment_runs WHERE id = ?",
                (row["experiment_run_id"],),
            ).fetchone()
            leg = conn.execute("SELECT seq, phase, started_at, ended_at FROM legs WHERE id = ?",
                               (run["leg_id"],)).fetchone()
            source["experiment"] = {"protocol_ref": run["protocol_ref"],
                                    "parameters": json.loads(run["parameters"]),
                                    "ran_at": run["ran_at"], "leg": dict(leg)}
        handovers = conn.execute(
            "SELECT seq, from_party, to_party, handed_at, condition"
            " FROM sample_handovers WHERE sample_id = ? ORDER BY seq", (sid,)).fetchall()
        source["handovers"] = [dict(h) for h in handovers]
        samples.append(source)
    return {"org_ref": org_ref, "count": len(samples), "samples": samples}

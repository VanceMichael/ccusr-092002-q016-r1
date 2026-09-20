-- 科研载荷海试验收：航次、航段、试航科目、装船仪器与证据链
-- 所有结论必须由同一航次下挂接的观测/实验证据支撑，结构上禁止跨航次借用结论。

CREATE TABLE IF NOT EXISTS voyages (
    voyage_id    TEXT PRIMARY KEY,
    vessel_ref   TEXT NOT NULL,
    title        TEXT NOT NULL,
    sea_area_ref TEXT,
    departure_at TEXT NOT NULL,
    returned_at  TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voyage_legs (
    leg_id     TEXT PRIMARY KEY,
    voyage_id  TEXT NOT NULL REFERENCES voyages(voyage_id),
    leg_seq    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    UNIQUE(voyage_id, leg_seq)
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   TEXT PRIMARY KEY,
    vessel_ref      TEXT NOT NULL,
    name            TEXT NOT NULL,
    yard_record_ref TEXT,            -- 船厂港口单机合格记录编号，仅作来源材料
    installed_at    TEXT
);

CREATE TABLE IF NOT EXISTS calibration_certificates (
    cert_id       TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    cert_ref      TEXT NOT NULL,
    issued_by     TEXT,
    calibrated_at TEXT NOT NULL,
    valid_until   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    subject_id    TEXT PRIMARY KEY,
    voyage_id     TEXT NOT NULL REFERENCES voyages(voyage_id),
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING', 'PASSED', 'RETEST_REQUIRED')),
    retest_reason TEXT,
    decided_by    TEXT,
    decided_at    TEXT,
    UNIQUE(voyage_id, code)
);

CREATE TABLE IF NOT EXISTS subject_instruments (
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
    PRIMARY KEY (subject_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS observation_windows (
    window_id  TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
    leg_id     TEXT NOT NULL REFERENCES voyage_legs(leg_id),
    started_at TEXT NOT NULL,
    ended_at   TEXT NOT NULL,
    note       TEXT,
    recorded_by TEXT
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    run_id               TEXT PRIMARY KEY,
    subject_id           TEXT NOT NULL REFERENCES subjects(subject_id),
    run_seq              INTEGER NOT NULL,
    procedure_ref        TEXT NOT NULL,   -- 可复现实验程序编号
    params_json          TEXT NOT NULL DEFAULT '{}',
    operator_ref         TEXT,
    recorded_at          TEXT NOT NULL,
    reproducibility_note TEXT,
    UNIQUE(subject_id, run_seq)
);

CREATE TABLE IF NOT EXISTS readings (
    reading_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES experiment_runs(run_id),
    metric       TEXT NOT NULL,
    raw_value    TEXT NOT NULL,          -- 原始读数永不覆盖
    unit         TEXT,
    reading_at   TEXT NOT NULL,
    anomaly      INTEGER NOT NULL DEFAULT 0 CHECK (anomaly IN (0, 1)),
    reviewed_by  TEXT,                   -- 异常数据复核前可空，判合格前必须补齐
    review_note  TEXT,
    reviewed_at  TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id      TEXT PRIMARY KEY,
    sample_ref     TEXT NOT NULL UNIQUE,
    run_id         TEXT NOT NULL REFERENCES experiment_runs(run_id),
    collected_at   TEXT NOT NULL,
    specimen_desc  TEXT,
    condition_note TEXT
);

CREATE TABLE IF NOT EXISTS sample_handovers (
    handover_id    TEXT PRIMARY KEY,
    sample_id      TEXT NOT NULL REFERENCES samples(sample_id),
    handover_seq   INTEGER NOT NULL,
    from_party     TEXT NOT NULL,
    to_party       TEXT NOT NULL,
    receiver_ref   TEXT NOT NULL,
    handed_at      TEXT NOT NULL,
    location       TEXT,
    condition_note TEXT,
    UNIQUE(sample_id, handover_seq)
);

CREATE TABLE IF NOT EXISTS sample_access_grants (
    grant_id        TEXT PRIMARY KEY,
    sample_id       TEXT NOT NULL REFERENCES samples(sample_id),
    institution_ref TEXT NOT NULL,
    scope_note      TEXT,
    granted_by      TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    UNIQUE(sample_id, institution_ref)
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_science_payload_acceptance');

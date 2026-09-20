-- 科研载荷海试验收：航次/航段、科目、仪器、证书、观测、样本、读数与复核
-- 所有业务表只追加软状态，原始读数与交接链不得物理删除。

CREATE TABLE IF NOT EXISTS voyages (
    id              TEXT PRIMARY KEY,          -- 航次编号，如 VOY-2026-01
    vessel_ref      TEXT NOT NULL,
    sea_area_ref    TEXT,
    departure_at    TEXT NOT NULL,             -- ISO 8601 with offset
    arrived_at      TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS legs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voyage_id       TEXT NOT NULL REFERENCES voyages(id),
    seq             INTEGER NOT NULL,          -- 航段在该航次内的顺序
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    phase           TEXT NOT NULL DEFAULT '海试',
    UNIQUE (voyage_id, seq)
);

-- 装船仪器（走航采样仪、冷藏样本舱、定位设备等）
CREATE TABLE IF NOT EXISTS instruments (
    id              TEXT PRIMARY KEY,          -- 仪器编号
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    installed_voyage_id TEXT REFERENCES voyages(id),
    UNIQUE (id)
);

-- 校准证书：明确所属航次，证书不跨航次冒用
CREATE TABLE IF NOT EXISTS calibration_certificates (
    id              TEXT PRIMARY KEY,
    instrument_id   TEXT NOT NULL REFERENCES instruments(id),
    voyage_id       TEXT NOT NULL REFERENCES voyages(id),
    issuer          TEXT NOT NULL,
    calibrated_at   TEXT NOT NULL,
    valid_until     TEXT,
    result          TEXT NOT NULL DEFAULT '合格'
                   CHECK (result IN ('合格', '限用', '停用'))
);

-- 试航科目
CREATE TABLE IF NOT EXISTS subjects (
    id              TEXT PRIMARY KEY,          -- 科目编号，如 SUBJ-TRAWL-01
    voyage_id       TEXT NOT NULL REFERENCES voyages(id),
    name            TEXT NOT NULL,
    requirement     TEXT,
    status          TEXT NOT NULL DEFAULT '进行中'
                    CHECK (status IN ('进行中', '已完成', '未通过', '待复测'))
);

CREATE TABLE IF NOT EXISTS subject_instruments (
    subject_id      TEXT NOT NULL REFERENCES subjects(id),
    instrument_id   TEXT NOT NULL REFERENCES instruments(id),
    PRIMARY KEY (subject_id, instrument_id)
);

-- 海上观测时间窗（仪器在科目中的实际工作时段，落在某航段内）
CREATE TABLE IF NOT EXISTS observation_windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id      TEXT NOT NULL REFERENCES subjects(id),
    leg_id          INTEGER NOT NULL REFERENCES legs(id),
    instrument_id   TEXT NOT NULL REFERENCES instruments(id),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    observer        TEXT NOT NULL
);

-- 可复现实验记录：协议版本 + 参数快照，保证实验可复现
CREATE TABLE IF NOT EXISTS experiment_runs (
    id              TEXT PRIMARY KEY,
    subject_id      TEXT NOT NULL REFERENCES subjects(id),
    leg_id          INTEGER NOT NULL REFERENCES legs(id),
    instrument_id   TEXT NOT NULL REFERENCES instruments(id),
    protocol_ref    TEXT NOT NULL,             -- 实验规程版本
    parameters     TEXT NOT NULL DEFAULT '{}', -- JSON 参数快照
    operator        TEXT NOT NULL,
    ran_at          TEXT NOT NULL
);

-- 样本：冷藏样本舱内的样本，来源可追溯
CREATE TABLE IF NOT EXISTS samples (
    id              TEXT PRIMARY KEY,
    subject_id      TEXT NOT NULL REFERENCES subjects(id),
    experiment_run_id TEXT REFERENCES experiment_runs(id),
    storage_instrument_id TEXT NOT NULL REFERENCES instruments(id),
    collected_at    TEXT NOT NULL,
    collected_by    TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);

-- 样本交接链（只追加）
CREATE TABLE IF NOT EXISTS sample_handovers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL REFERENCES samples(id),
    seq             INTEGER NOT NULL,
    from_party      TEXT NOT NULL,
    to_party        TEXT NOT NULL,
    handed_at       TEXT NOT NULL,
    condition       TEXT NOT NULL DEFAULT '完好',
    UNIQUE (sample_id, seq)
);

-- 实验读数：原始读数不可变；异常读数须登记原始值与复核人
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_run_id TEXT NOT NULL REFERENCES experiment_runs(id),
    metric          TEXT NOT NULL,
    raw_value       TEXT NOT NULL,              -- 原始读数，原样保留
    unit            TEXT,
    recorded_at     TEXT NOT NULL,
    is_anomaly      INTEGER NOT NULL DEFAULT 0,
    anomaly_reason  TEXT,
    reviewed_by     TEXT,                       -- 异常数据复核人
    reviewed_at     TEXT,
    review_result   TEXT CHECK (review_result IN ('确认异常', '误报修正'))
);

-- 仪器逐航次验证结论：一台仪器在一个航次内最多一条验收结论
CREATE TABLE IF NOT EXISTS instrument_validations (
    instrument_id   TEXT NOT NULL REFERENCES instruments(id),
    voyage_id       TEXT NOT NULL REFERENCES voyages(id),
    leg_id          INTEGER REFERENCES legs(id),
    result          TEXT NOT NULL
                    CHECK (result IN ('通过', '待复测', '不通过')),
    reason          TEXT,                        -- 仍需复测/不通过的原因
    decided_by      TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    PRIMARY KEY (instrument_id, voyage_id)
);

-- 研究机构对样本的追溯授权（只能查看授权范围内样本来源）
CREATE TABLE IF NOT EXISTS sample_grants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_ref         TEXT NOT NULL,              -- 研究机构标识
    sample_id       TEXT REFERENCES samples(id), -- 空范围 = 整科目授权（见 subject_id）
    subject_id      TEXT REFERENCES subjects(id),
    granted_by      TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    CHECK (sample_id IS NOT NULL OR subject_id IS NOT NULL)
);

-- 访问令牌：角色决定可见范围
CREATE TABLE IF NOT EXISTS access_tokens (
    token           TEXT PRIMARY KEY,
    role            TEXT NOT NULL
                    CHECK (role IN ('scientist', 'acceptance', 'owner', 'researcher')),
    party_ref       TEXT NOT NULL,              -- 机构/人员标识，如研究机构编号
    label           TEXT
);

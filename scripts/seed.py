
"""演示种子：一次海试航次、三类载荷仪器、四个角色令牌。

重复执行安全（INSERT OR IGNORE）。仅使用虚构人员与机构标识。
"""

import sqlite3
from pathlib import Path

from scripts.migrate import run_migrations

VOYAGE = "VOY-2026-01"

TOKENS = [
    ("SCI-TOKEN", "scientist", "SCI-TEAM", "海洋科研团队"),
    ("ACC-TOKEN", "acceptance", "ACC-LI", "靠港验收人员"),
    ("OWNER-TOKEN", "owner", "OWNER-COOP", "渔民船东"),
    ("RI-TOKEN", "researcher", "RI-DEEPSEA", "深海研究机构"),
]

INSTRUMENTS = [
    ("TRAWL-S01", "走航采样仪", "采样"),
    ("COLD-C01", "冷藏样本舱", "存储"),
    ("GPS-P01", "定位设备", "定位"),
]


def seed(database_path: Path) -> None:
    run_migrations(database_path)
    with sqlite3.connect(database_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO access_tokens(token, role, party_ref, label)"
            " VALUES (?,?,?,?)",
            TOKENS,
        )
        conn.execute(
            "INSERT OR IGNORE INTO voyages(id, vessel_ref, sea_area_ref, departure_at, note)"
            " VALUES (?,?,?,?,?)",
            (VOYAGE, "VESSEL-DEMO", "AREA-A", "2026-09-10T08:00:00+08:00", "首次海试"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO legs(voyage_id, seq, started_at, ended_at, phase)"
            " VALUES (?,?,?,?,?)",
            (VOYAGE, 1, "2026-09-10T08:30:00+08:00", "2026-09-10T14:00:00+08:00", "港外海试"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO legs(voyage_id, seq, started_at, ended_at, phase)"
            " VALUES (?,?,?,?,?)",
            (VOYAGE, 2, "2026-09-10T15:00:00+08:00", "2026-09-10T19:30:00+08:00", "返航观测"),
        )
        for iid, name, category in INSTRUMENTS:
            conn.execute(
                "INSERT OR IGNORE INTO instruments(id, name, category, installed_voyage_id)"
                " VALUES (?,?,?,?)",
                (iid, name, category, VOYAGE),
            )
            conn.execute(
                "INSERT OR IGNORE INTO calibration_certificates"
                "(id, instrument_id, voyage_id, issuer, calibrated_at, valid_until, result)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"CERT-{iid}", iid, VOYAGE, "船厂计量站",
                 "2026-09-08T10:00:00+08:00", "2027-09-08T10:00:00+08:00", "合格"),
            )
        # 两个试航科目
        conn.execute(
            "INSERT OR IGNORE INTO subjects(id, voyage_id, name, requirement, status)"
            " VALUES (?,?,?,?, '进行中')",
            ("SUBJ-SAMPLE-01", VOYAGE, "走航采样与冷藏联动", "采样-入舱温度链全程可追溯"),
        )
        for iid in ("TRAWL-S01", "COLD-C01"):
            conn.execute(
                "INSERT OR IGNORE INTO subject_instruments(subject_id, instrument_id)"
                " VALUES (?,?)", ("SUBJ-SAMPLE-01", iid),
            )
        conn.execute(
            "INSERT OR IGNORE INTO subjects(id, voyage_id, name, requirement, status)"
            " VALUES (?,?,?,?, '进行中')",
            ("SUBJ-NAV-01", VOYAGE, "定位设备走航验证", "连续定位且坐标可复现"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO subject_instruments(subject_id, instrument_id)"
            " VALUES (?,?)", ("SUBJ-NAV-01", "GPS-P01"),
        )
        # 海上观测时间窗
        windows = [
            ("SUBJ-SAMPLE-01", 1, "TRAWL-S01", "2026-09-10T09:00:00+08:00",
             "2026-09-10T12:00:00+08:00", "王观测"),
            ("SUBJ-SAMPLE-01", 1, "COLD-C01", "2026-09-10T09:10:00+08:00",
             "2026-09-10T13:50:00+08:00", "王观测"),
            ("SUBJ-NAV-01", 2, "GPS-P01", "2026-09-10T15:10:00+08:00",
             "2026-09-10T19:00:00+08:00", "陈观测"),
        ]
        conn.executemany(
            "INSERT INTO observation_windows"
            "(subject_id, leg_id, instrument_id, started_at, ended_at, observer)"
            " SELECT ?,?,?,?,?,? WHERE NOT EXISTS ("
            " SELECT 1 FROM observation_windows WHERE subject_id=? AND instrument_id=?)",
            [(s, l, i, st, en, ob, s, i) for s, l, i, st, en, ob in windows],
        )
        # 可复现实验记录
        runs = [
            ("RUN-S-01", "SUBJ-SAMPLE-01", 1, "TRAWL-S01", "PROTO-TRAWL-v2.1",
             '{"拖速节": 4.5, "网深米": 20}', "王观测", "2026-09-10T10:00:00+08:00"),
            ("RUN-C-01", "SUBJ-SAMPLE-01", 1, "COLD-C01", "PROTO-COLD-v1.4",
             '{"设定温度_C": -20, "记录间隔秒": 60}', "王观测",
             "2026-09-10T10:05:00+08:00"),
            ("RUN-N-01", "SUBJ-NAV-01", 2, "GPS-P01", "PROTO-NAV-v3.0",
             '{"采样间隔秒": 1, "坐标系": "WGS84"}', "陈观测",
             "2026-09-10T15:30:00+08:00"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO experiment_runs"
            "(id, subject_id, leg_id, instrument_id, protocol_ref, parameters, operator, ran_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            runs,
        )
        # 读数：含一条已复核异常（温度瞬时跳变）
        readings = [
            ("RUN-S-01", "渔获重量_kg", "12.4", "kg", "2026-09-10T10:30:00+08:00", 0,
             None, None, None, None),
            ("RUN-C-01", "舱温_C", "-20.1", "°C", "2026-09-10T10:30:00+08:00", 0,
             None, None, None, None),
            ("RUN-C-01", "舱温_C", "8.7", "°C", "2026-09-10T11:02:00+08:00", 1,
             "开舱取样时温度瞬时跳变，原始读数保留", "李复核",
             "2026-09-10T11:20:00+08:00", "确认异常"),
            ("RUN-N-01", "定位偏差_m", "1.8", "m", "2026-09-10T16:00:00+08:00", 0,
             None, None, None, None),
        ]
        for r in readings:
            conn.execute(
                "INSERT INTO readings"
                "(experiment_run_id, metric, raw_value, unit, recorded_at, is_anomaly,"
                " anomaly_reason, reviewed_by, reviewed_at, review_result)"
                " SELECT ?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS ("
                " SELECT 1 FROM readings WHERE experiment_run_id=? AND metric=? AND recorded_at=?)",
                (*r, r[0], r[1], r[4]),
            )
        # 样本与交接链
        conn.execute(
            "INSERT OR IGNORE INTO samples"
            "(id, subject_id, experiment_run_id, storage_instrument_id,"
            " collected_at, collected_by, metadata_json)"
            " VALUES (?,?,?,?,?,?,?)",
            ("SMP-01", "SUBJ-SAMPLE-01", "RUN-S-01", "COLD-C01",
             "2026-09-10T10:35:00+08:00", "王观测",
             '{"物种": "示范鱼种", "站点": "AREA-A-S07"}'),
        )
        handovers = [
            ("SMP-01", 1, "采样组", "冷藏管理员", "2026-09-10T10:40:00+08:00", "完好"),
            ("SMP-01", 2, "冷藏管理员", "靠港样本接收员",
             "2026-09-10T20:10:00+08:00", "冷链完整"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO sample_handovers"
            "(sample_id, seq, from_party, to_party, handed_at, condition)"
            " VALUES (?,?,?,?,?,?)",
            handovers,
        )
        # 研究机构仅被授权追溯该样本
        conn.execute(
            "INSERT INTO sample_grants(org_ref, sample_id, subject_id, granted_by, granted_at)"
            " SELECT 'RI-DEEPSEA','SMP-01',NULL,'SCI-TEAM','2026-09-11T09:00:00+08:00'"
            " WHERE NOT EXISTS (SELECT 1 FROM sample_grants WHERE org_ref='RI-DEEPSEA')",
        )
        conn.commit()
    print(f"演示数据就绪：{database_path}")
    print("令牌：", ", ".join(t[0] for t in TOKENS))


if __name__ == "__main__":
    import os
    seed(Path(os.getenv("DATABASE_PATH", "data/app.sqlite3")))

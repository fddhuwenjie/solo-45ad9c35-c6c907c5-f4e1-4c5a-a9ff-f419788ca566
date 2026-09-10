"""sqlite3 存储：数据集、数据点、分析版本、剔除记录。"""

from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("RESPIRO_DB",
                         os.path.join(os.path.dirname(__file__), "respiro.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sample_id TEXT NOT NULL DEFAULT '',
    is_blank INTEGER NOT NULL DEFAULT 0,
    filename TEXT DEFAULT '',
    uploaded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS points (
    dataset_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    t REAL NOT NULL,
    o2 REAL NOT NULL,
    temp REAL, press REAL, vol REAL,
    event TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_points_ds ON points(dataset_id, idx);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    note TEXT DEFAULT '',
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_ds ON versions(dataset_id, id);
CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def create_dataset(conn, name, sample_id, is_blank, filename, points):
    cur = conn.execute(
        "INSERT INTO datasets(name, sample_id, is_blank, filename, uploaded_at)"
        " VALUES (?,?,?,?,?)",
        (name, sample_id, 1 if is_blank else 0, filename, time.time()))
    ds_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO points(dataset_id, idx, t, o2, temp, press, vol, event)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(ds_id, i, p["t"], p["o2"], p.get("temp"), p.get("press"),
          p.get("vol"), p.get("event", "")) for i, p in enumerate(points)])
    conn.commit()
    return ds_id


def list_datasets(conn):
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM points p WHERE p.dataset_id=d.id)"
        " AS n_points FROM datasets d ORDER BY d.id").fetchall()
    return [dict(r) for r in rows]


def get_dataset(conn, ds_id):
    row = conn.execute("SELECT * FROM datasets WHERE id=?", (ds_id,)).fetchone()
    return dict(row) if row else None


def get_points(conn, ds_id):
    rows = conn.execute(
        "SELECT t, o2, temp, press, vol, event FROM points"
        " WHERE dataset_id=? ORDER BY idx", (ds_id,)).fetchall()
    return [dict(r) for r in rows]


def delete_dataset(conn, ds_id):
    for tbl in ("points", "versions", "exclusions"):
        conn.execute(f"DELETE FROM {tbl} WHERE dataset_id=?", (ds_id,))
    conn.execute("DELETE FROM datasets WHERE id=?", (ds_id,))
    conn.commit()


# ------------------------------------------------------------------ 版本

def save_version(conn, ds_id, params, result, note=""):
    # 结果中的点列体积大且可由参数重算，版本里只留统计量
    slim = {k: v for k, v in result.items()
            if k not in ("corrected", "window_points", "residuals")}
    conn.execute(
        "INSERT INTO versions(dataset_id, created_at, note, params_json,"
        " result_json) VALUES (?,?,?,?,?)",
        (ds_id, time.time(), note, json.dumps(params, ensure_ascii=False),
         json.dumps(slim, ensure_ascii=False)))
    conn.commit()


def list_versions(conn, ds_id):
    rows = conn.execute(
        "SELECT id, created_at, note, params_json, result_json FROM versions"
        " WHERE dataset_id=? ORDER BY id", (ds_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "created_at": r["created_at"], "note": r["note"],
            "params": json.loads(r["params_json"]),
            "result": json.loads(r["result_json"]),
        })
    return out


def undo_version(conn, ds_id):
    """删除最新版本并回退到上一版；返回 (ok, message, current)。"""
    rows = conn.execute(
        "SELECT id FROM versions WHERE dataset_id=? ORDER BY id", (ds_id,)
    ).fetchall()
    if not rows:
        return False, "没有可撤销的版本", None
    if len(rows) == 1:
        return False, "仅剩初始版本，不能再撤销", None
    conn.execute("DELETE FROM versions WHERE id=?", (rows[-1]["id"],))
    conn.commit()
    current = conn.execute(
        "SELECT params_json, result_json FROM versions WHERE id=?",
        (rows[-2]["id"],)).fetchone()
    return True, "已撤销到上一版本", {
        "params": json.loads(current["params_json"]),
        "result": json.loads(current["result_json"]),
    }


# ------------------------------------------------------------------ 剔除

def add_exclusion(conn, ds_id, reason):
    conn.execute(
        "INSERT INTO exclusions(dataset_id, reason, created_at) VALUES (?,?,?)",
        (ds_id, reason, time.time()))
    conn.commit()


def remove_exclusion(conn, exclusion_id):
    conn.execute("DELETE FROM exclusions WHERE id=?", (exclusion_id,))
    conn.commit()


def list_exclusions(conn):
    rows = conn.execute(
        "SELECT e.id, e.dataset_id, e.reason, e.created_at, d.name, d.sample_id"
        " FROM exclusions e JOIN datasets d ON d.id=e.dataset_id"
        " ORDER BY e.id").fetchall()
    return [dict(r) for r in rows]

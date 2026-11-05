import re
import sqlite3
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from config import DB_PATH, MIN_WITHDRAW, REF_REWARD, SEED_TARGET, SUB_HOLD_DAYS, TASK_REWARD

SUB_CHECK_INTERVAL = 2 * 3600  # как часто перепроверять подписку (сек.)

_conn = sqlite3.connect(DB_PATH)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
        referrer_id INTEGER,
        ref_count INTEGER NOT NULL DEFAULT 0,
        banned INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        resolved_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('channel','bot')),
        url TEXT NOT NULL,
        chat_id INTEGER,
        target INTEGER NOT NULL CHECK (target > 0),
        done_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        proof_file_id TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE (user_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        expires_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ad_messages (
        ad_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        url TEXT NOT NULL,
        chat_id INTEGER,
        need_tasks INTEGER NOT NULL DEFAULT 0,
        qty INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        task_id INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tiktok_proofs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        proof_file_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """
)
for _sql in (
    "ALTER TABLE withdrawals ADD COLUMN resolved_at INTEGER",
    "ALTER TABLE users ADD COLUMN withdraw_blocked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN need_tasks INTEGER NOT NULL DEFAULT 2",
    "ALTER TABLE user_tasks ADD COLUMN approved_at INTEGER",
    "ALTER TABLE user_tasks ADD COLUMN last_check INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE user_tasks ADD COLUMN unsubscribed INTEGER NOT NULL DEFAULT 0",
):
    try:
        _conn.execute(_sql)
    except sqlite3.OperationalError:
        pass
_conn.execute(
    "UPDATE user_tasks SET approved_at=created_at WHERE status='approved' AND approved_at IS NULL"
)
_conn.commit()


def now() -> int:
    return int(time.time())


def fmt(x: int) -> str:
    return f"{x / 100:.2f}".rstrip("0").rstrip(".")


def parse_duration(text: str):
    t = (text or "").lower().strip()
    m = re.fullmatch(r"(?:(\d{1,3})\s*д)?\s*(?:(\d{1,4})\s*ч)?\s*(?:(\d{1,5})\s*м)?", t)
    if not m or not any(m.groups()):
        return None
    sec = int(timedelta(
        days=int(m.group(1) or 0), hours=int(m.group(2) or 0), minutes=int(m.group(3) or 0)
    ).total_seconds())
    if sec <= 0 or sec > 30 * 86400:
        return None
    return sec


def parse_amount(text: str):
    try:
        v = Decimal((text or "").strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not v.is_finite() or v <= 0 or v > 1_000_000:
        return None
    cents = v * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


def fmt_duration(sec: int) -> str:
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d}д" if d else "", f"{h}ч" if h else "", f"{m}м" if m else ""]
    return " ".join(p for p in parts if p) or "0м"


def valid_link(url: str) -> bool:
    return bool(re.fullmatch(r"https://t\.me/[A-Za-z0-9_+\-/?=&.%]{2,180}", url or ""))


def ensure_user(uid: int, username, first_name: str):
    with _conn:
        _conn.execute(
            "INSERT OR IGNORE INTO users(id, username, first_name, created_at) VALUES(?,?,?,?)",
            (uid, username, first_name, now()),
        )
        _conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE id=?", (username, first_name, uid)
        )


def register_user(uid: int, username, first_name: str, referrer_id):
    with _conn:
        if _conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            return False, None
        ref = None
        if referrer_id and referrer_id != uid:
            if _conn.execute(
                "SELECT 1 FROM users WHERE id=? AND banned=0", (referrer_id,)
            ).fetchone():
                ref = referrer_id
        _conn.execute(
            "INSERT INTO users(id, username, first_name, referrer_id, created_at) VALUES(?,?,?,?,?)",
            (uid, username, first_name, ref, now()),
        )
        if ref:
            _conn.execute(
                "UPDATE users SET balance=balance+?, ref_count=ref_count+1 WHERE id=?",
                (REF_REWARD, ref),
            )
        return True, ref


def get_user(uid: int):
    return _conn.execute(
        "SELECT balance, ref_count, banned FROM users WHERE id=?", (uid,)
    ).fetchone()


def is_banned(uid: int) -> bool:
    row = _conn.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def set_ban(uid: int, flag: bool) -> bool:
    with _conn:
        return _conn.execute("UPDATE users SET banned=? WHERE id=?", (int(flag), uid)).rowcount > 0


def user_names(uid: int):
    row = _conn.execute("SELECT username, first_name FROM users WHERE id=?", (uid,)).fetchone()
    return row if row else (None, None)


def withdraw_blocked(uid: int) -> bool:
    row = _conn.execute("SELECT withdraw_blocked FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def set_withdraw_block(uid: int, flag: bool) -> bool:
    with _conn:
        return (
            _conn.execute(
                "UPDATE users SET withdraw_blocked=? WHERE id=?", (int(flag), uid)
            ).rowcount > 0
        )


def adjust_balance(uid: int, delta: int):
    with _conn:
        row = _conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            return "nouser", None
        if row[0] + delta < 0:
            return "insufficient", row[0]
        _conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (delta, uid))
        return "ok", row[0] + delta


def all_user_ids():
    return [r[0] for r in _conn.execute("SELECT id FROM users WHERE banned=0")]


_FREE = (
    "(t.target - t.done_count - "
    "(SELECT COUNT(*) FROM user_tasks p WHERE p.task_id=t.id AND p.status='pending'))"
)

SEED_CHANNELS = [
    "https://t.me/+hNTsqRuALZFjZjc6",
    "https://t.me/+uDSAB7UseORhNWQ6",
    "https://t.me/+ACMWQOy5IjkzZDUy",
    "https://t.me/+DEsQUTEKyLk0NmZi",
    "https://t.me/+QbEBPHQZ6ugxZTVi",
]
KNOWN_CHAT_IDS = {
    "https://t.me/+uDSAB7UseORhNWQ6": -1004332989829,
    "https://t.me/+ACMWQOy5IjkzZDUy": -1003833596204,
    "https://t.me/+DEsQUTEKyLk0NmZi": -1003927344674,
    "https://t.me/+QbEBPHQZ6ugxZTVi": -1004396380281,
}
SEED_BOTS = [
    "https://t.me/StarsovEarnBot?start=o3U329Xqa",
    "https://t.me/StarsovComBot?start=y3VnE86AG",
    "https://t.me/StarsllyBot?start=ref_8037758285",
    "https://t.me/UnderRouletteBot?startapp=ref_S1M1N9HQ77",
    "https://t.me/milky_way_stars_bot?start=ref_8037758285",
    "https://t.me/KtoTutRobot?start=ref_8037758285",
    "https://t.me/JabaMarketApp?start=ref8037758285",
]


def seed_tasks() -> int:
    with _conn:
        if _conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
            return 0
        for kind, urls in (("bot", SEED_BOTS), ("channel", SEED_CHANNELS)):
            for url in urls:
                _conn.execute(
                    "INSERT INTO tasks(kind, url, chat_id, target, created_at) VALUES(?,?,?,?,?)",
                    (kind, url, KNOWN_CHAT_IDS.get(url), SEED_TARGET, now()),
                )
        _conn.execute("INSERT INTO meta(key, value) VALUES('seeded','1')")
        return len(SEED_BOTS) + len(SEED_CHANNELS)


def apply_known_chat_ids() -> int:
    n = 0
    with _conn:
        for url, cid in KNOWN_CHAT_IDS.items():
            n += _conn.execute(
                "UPDATE tasks SET chat_id=? WHERE url=? AND chat_id IS NULL AND status!='deleted'",
                (cid, url),
            ).rowcount
    return n


def url_active(url: str) -> bool:
    return bool(
        _conn.execute("SELECT 1 FROM tasks WHERE url=? AND status='active'", (url,)).fetchone()
    )


def add_task(kind: str, url: str, target: int, chat_id=None, need_tasks: int = 2):
    with _conn:
        if _conn.execute("SELECT 1 FROM tasks WHERE url=? AND status='active'", (url,)).fetchone():
            return None
        cur = _conn.execute(
            "INSERT INTO tasks(kind, url, chat_id, target, need_tasks, created_at) VALUES(?,?,?,?,?,?)",
            (kind, url, chat_id, target, need_tasks, now()),
        )
        return cur.lastrowid


def list_active_tasks():
    return _conn.execute(
        "SELECT id, kind, url, chat_id, target, done_count FROM tasks WHERE status='active' ORDER BY id"
    ).fetchall()


def get_task(tid: int):
    return _conn.execute(
        "SELECT id, kind, url, chat_id, target, done_count, status FROM tasks WHERE id=?", (tid,)
    ).fetchone()


def delete_task(tid: int) -> bool:
    with _conn:
        return _conn.execute(
            "UPDATE tasks SET status='deleted' WHERE id=? AND status!='deleted'", (tid,)
        ).rowcount > 0


def set_target(tid: int, n: int) -> bool:
    with _conn:
        row = _conn.execute("SELECT done_count, status FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row or row[1] == "deleted" or n < 1:
            return False
        _conn.execute(
            "UPDATE tasks SET target=?, status=? WHERE id=?",
            (n, "done" if n <= row[0] else "active", tid),
        )
        return True


def set_chat_id(tid: int, chat_id: int) -> bool:
    with _conn:
        return _conn.execute(
            "UPDATE tasks SET chat_id=? WHERE id=? AND kind='channel' AND status!='deleted'",
            (chat_id, tid),
        ).rowcount > 0


def available_tasks(uid: int, limit: int = 20):
    return _conn.execute(
        "SELECT t.id, t.kind FROM tasks t WHERE t.status='active' AND "
        + _FREE
        + " > 0 AND NOT EXISTS (SELECT 1 FROM user_tasks u WHERE u.task_id=t.id "
        "AND u.user_id=? AND u.status IN ('pending','approved')) ORDER BY t.id LIMIT ?",
        (uid, limit),
    ).fetchall()


def task_for_user(uid: int, tid: int):
    return _conn.execute(
        "SELECT t.id, t.kind, t.url, t.chat_id, t.need_tasks FROM tasks t "
        "WHERE t.id=? AND t.status='active' AND "
        + _FREE
        + " > 0 AND NOT EXISTS (SELECT 1 FROM user_tasks u WHERE u.task_id=t.id "
        "AND u.user_id=? AND u.status IN ('pending','approved'))",
        (tid, uid),
    ).fetchone()


def pending_count(uid: int) -> int:
    return _conn.execute(
        "SELECT COUNT(*) FROM user_tasks WHERE user_id=? AND status='pending'", (uid,)
    ).fetchone()[0]


def approved_count(uid: int) -> int:
    return _conn.execute(
        "SELECT COUNT(*) FROM user_tasks WHERE user_id=? AND status='approved'", (uid,)
    ).fetchone()[0]


def _credit(uid: int, tid: int):
    _conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (TASK_REWARD, uid))
    _conn.execute("UPDATE tasks SET done_count=done_count+1 WHERE id=?", (tid,))
    _conn.execute(
        "UPDATE tasks SET status='done' WHERE id=? AND status='active' AND done_count>=target", (tid,)
    )


def _slots(tid: int):
    return _conn.execute(
        "SELECT status, target - done_count - "
        "(SELECT COUNT(*) FROM user_tasks WHERE task_id=? AND status='pending') FROM tasks WHERE id=?",
        (tid, tid),
    ).fetchone()


def claim_auto(uid: int, tid: int) -> str:
    with _conn:
        if is_banned(uid):
            return "banned"
        t = _slots(tid)
        if not t or t[0] != "active":
            return "inactive"
        row = _conn.execute(
            "SELECT id, status FROM user_tasks WHERE user_id=? AND task_id=?", (uid, tid)
        ).fetchone()
        if row and row[1] in ("pending", "approved"):
            return "already"
        if t[1] <= 0:
            return "full"
        if row:
            _conn.execute(
                "UPDATE user_tasks SET status='approved', approved_at=?, last_check=?, unsubscribed=0 WHERE id=?",
                (now(), now(), row[0]),
            )
        else:
            _conn.execute(
                "INSERT INTO user_tasks(user_id, task_id, status, created_at, approved_at, last_check) VALUES(?,?,'approved',?,?,?)",
                (uid, tid, now(), now(), now()),
            )
        _credit(uid, tid)
        return "ok"


def submit_proof(uid: int, tid: int, file_id: str):
    with _conn:
        if is_banned(uid):
            return "banned", None
        t = _slots(tid)
        if not t or t[0] != "active":
            return "inactive", None
        row = _conn.execute(
            "SELECT id, status FROM user_tasks WHERE user_id=? AND task_id=?", (uid, tid)
        ).fetchone()
        if row and row[1] in ("pending", "approved"):
            return "already", None
        if t[1] <= 0:
            return "full", None
        if _conn.execute(
            "SELECT COUNT(*) FROM user_tasks WHERE user_id=? AND status='pending'", (uid,)
        ).fetchone()[0] >= 3:
            return "limit", None
        if row:
            _conn.execute(
                "UPDATE user_tasks SET status='pending', proof_file_id=?, created_at=? WHERE id=?",
                (file_id, now(), row[0]),
            )
            return "ok", row[0]
        cur = _conn.execute(
            "INSERT INTO user_tasks(user_id, task_id, status, proof_file_id, created_at) VALUES(?,?,'pending',?,?)",
            (uid, tid, file_id, now()),
        )
        return "ok", cur.lastrowid


def resolve_proof(utid: int, approve: bool):
    with _conn:
        row = _conn.execute(
            "SELECT user_id, task_id FROM user_tasks WHERE id=? AND status='pending'", (utid,)
        ).fetchone()
        if not row:
            return None
        if approve:
            _conn.execute(
                "UPDATE user_tasks SET status='approved', approved_at=? WHERE id=?", (now(), utid)
            )
            _credit(row[0], row[1])
        else:
            _conn.execute("UPDATE user_tasks SET status='rejected' WHERE id=?", (utid,))
        return row


def pending_proofs(limit: int = 5):
    return _conn.execute(
        "SELECT ut.id, ut.user_id, u.username, u.first_name, t.id, t.kind, t.url, ut.proof_file_id "
        "FROM user_tasks ut JOIN tasks t ON t.id=ut.task_id LEFT JOIN users u ON u.id=ut.user_id "
        "WHERE ut.status='pending' ORDER BY ut.id LIMIT ?",
        (limit,),
    ).fetchall()


def create_withdrawal(uid: int):
    with _conn:
        row = _conn.execute(
            "SELECT balance FROM users WHERE id=? AND banned=0 AND withdraw_blocked=0", (uid,)
        ).fetchone()
        if not row or row[0] < MIN_WITHDRAW:
            return None
        amount = row[0]
        _conn.execute("UPDATE users SET balance=0 WHERE id=?", (uid,))
        cur = _conn.execute(
            "INSERT INTO withdrawals(user_id, amount, created_at) VALUES(?,?,?)", (uid, amount, now())
        )
        return cur.lastrowid, amount


def resolve_withdrawal(wid: int, approve: bool):
    with _conn:
        row = _conn.execute(
            "SELECT user_id, amount FROM withdrawals WHERE id=? AND status='pending'", (wid,)
        ).fetchone()
        if not row:
            return None
        _conn.execute(
            "UPDATE withdrawals SET status=?, resolved_at=? WHERE id=? AND status='pending'",
            ("paid" if approve else "rejected", now(), wid),
        )
        if not approve:
            _conn.execute("UPDATE users SET balance=balance+? WHERE id=?", (row[1], row[0]))
        return row


def pending_withdrawals(limit: int = 10):
    return _conn.execute(
        "SELECT w.id, w.user_id, u.username, u.first_name, w.amount, w.created_at "
        "FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id LIMIT ?",
        (limit,),
    ).fetchall()


def withdrawal_history(limit: int = 20):
    return _conn.execute(
        "SELECT w.id, w.user_id, u.username, w.amount, w.status, w.resolved_at "
        "FROM withdrawals w LEFT JOIN users u ON u.id=w.user_id WHERE w.status!='pending' ORDER BY w.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def stats():
    q = _conn.execute
    return {
        "users": q("SELECT COUNT(*) FROM users").fetchone()[0],
        "banned": q("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0],
        "balances": q("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0],
        "wd_pending": q("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'").fetchone(),
        "wd_paid": q("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'").fetchone(),
        "tasks": q("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0],
        "proofs": q("SELECT COUNT(*) FROM user_tasks WHERE status='pending'").fetchone()[0],
        "orders": q("SELECT COUNT(*) FROM orders WHERE status='waiting'").fetchone()[0],
        "tiktoks": q("SELECT COUNT(*) FROM tiktok_proofs WHERE status='pending'").fetchone()[0],
        "blocked": q("SELECT COUNT(*) FROM users WHERE withdraw_blocked=1").fetchone()[0],
    }


def create_ad(duration_sec: int) -> int:
    with _conn:
        cur = _conn.execute(
            "INSERT INTO ads(expires_at, created_at) VALUES(?,?)", (now() + duration_sec, now())
        )
        return cur.lastrowid


def record_ad_messages(ad_id: int, rows):
    if not rows:
        return
    with _conn:
        _conn.executemany(
            "INSERT INTO ad_messages(ad_id, chat_id, message_id) VALUES(?,?,?)",
            [(ad_id, c, m) for c, m in rows],
        )


def expired_ads():
    return [r[0] for r in _conn.execute(
        "SELECT id FROM ads WHERE status='active' AND expires_at<=?", (now(),)
    )]


def active_ads():
    return _conn.execute(
        "SELECT id, expires_at FROM ads WHERE status='active' ORDER BY id DESC LIMIT 20"
    ).fetchall()


def ad_messages(ad_id: int):
    return _conn.execute(
        "SELECT chat_id, message_id FROM ad_messages WHERE ad_id=?", (ad_id,)
    ).fetchall()


def mark_ad_deleted(ad_id: int):
    with _conn:
        _conn.execute("UPDATE ads SET status='deleted' WHERE id=?", (ad_id,))
        _conn.execute("DELETE FROM ad_messages WHERE ad_id=?", (ad_id,))


def due_sub_checks(limit: int = 20):
    t = now()
    return _conn.execute(
        "SELECT ut.id, ut.user_id, ut.task_id, t.chat_id FROM user_tasks ut "
        "JOIN tasks t ON t.id=ut.task_id "
        "WHERE ut.status='approved' AND ut.unsubscribed=0 AND t.kind='channel' "
        "AND t.chat_id IS NOT NULL AND ut.approved_at > ? AND ut.last_check < ? ORDER BY ut.last_check LIMIT ?",
        (t - SUB_HOLD_DAYS * 86400, t - SUB_CHECK_INTERVAL, limit),
    ).fetchall()


def touch_check(utid: int):
    with _conn:
        _conn.execute("UPDATE user_tasks SET last_check=? WHERE id=?", (now(), utid))


def mark_unsub(utid: int):
    with _conn:
        row = _conn.execute(
            "SELECT user_id, task_id FROM user_tasks WHERE id=? AND unsubscribed=0", (utid,)
        ).fetchone()
        if not row:
            return None
        _conn.execute("UPDATE user_tasks SET unsubscribed=1, last_check=? WHERE id=?", (now(), utid))
        _conn.execute("UPDATE users SET withdraw_blocked=1 WHERE id=?", (row[0],))
        return row


def create_order(uid: int, kind: str, url: str, chat_id, need_tasks: int, qty: int):
    with _conn:
        if _conn.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id=? AND status='waiting'", (uid,)
        ).fetchone()[0] >= 2:
            return None
        _conn.execute("DELETE FROM orders WHERE user_id=? AND status='new'", (uid,))
        cur = _conn.execute(
            "INSERT INTO orders(user_id, kind, url, chat_id, need_tasks, qty, created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, kind, url, chat_id, need_tasks, qty, now()),
        )
        return cur.lastrowid


def mark_paid(oid: int, uid: int) -> bool:
    with _conn:
        return _conn.execute(
            "UPDATE orders SET status='waiting' WHERE id=? AND user_id=? AND status='new'", (oid, uid)
        ).rowcount > 0


def get_order(oid: int):
    return _conn.execute(
        "SELECT id, user_id, kind, url, chat_id, need_tasks, qty, status FROM orders WHERE id=?", (oid,)
    ).fetchone()


def waiting_orders(limit: int = 10):
    return [r[0] for r in _conn.execute(
        "SELECT id FROM orders WHERE status='waiting' ORDER BY id LIMIT ?", (limit,)
    )]


def approve_order(oid: int):
    with _conn:
        row = _conn.execute(
            "SELECT user_id, kind, url, chat_id, need_tasks, qty FROM orders WHERE id=? AND status='waiting'", (oid,)
        ).fetchone()
        if not row:
            return None
        uid, kind, url, chat_id, need, qty = row
        ex = _conn.execute("SELECT id FROM tasks WHERE url=? AND status='active'", (url,)).fetchone()
        if ex:
            tid, merged = ex[0], True
            _conn.execute(
                "UPDATE tasks SET target=target+?, chat_id=COALESCE(chat_id, ?) WHERE id=?", (qty, chat_id, tid)
            )
        else:
            cur = _conn.execute(
                "INSERT INTO tasks(kind, url, chat_id, target, need_tasks, created_at) VALUES(?,?,?,?,?,?)",
                (kind, url, chat_id, qty, need, now()),
            )
            tid, merged = cur.lastrowid, False
        _conn.execute("UPDATE orders SET status='approved', task_id=? WHERE id=?", (tid, oid))
        return uid, tid, qty, kind, url, merged


def reject_order(oid: int):
    with _conn:
        row = _conn.execute(
            "SELECT user_id FROM orders WHERE id=? AND status='waiting'", (oid,)
        ).fetchone()
        if not row:
            return None
        _conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        return row[0]


# ------------------------------------------------------------ TikTok
TIKTOK_REWARD_CENTS = 10  # 0.10⭐


def submit_tiktok(uid: int, file_id: str) -> int:
    with _conn:
        cur = _conn.execute(
            "INSERT INTO tiktok_proofs(user_id, proof_file_id, created_at) VALUES(?,?,?)",
            (uid, file_id, now()),
        )
        return cur.lastrowid


def resolve_tiktok(utid: int, approve: bool):
    """Возвращает user_id или None если уже обработано."""
    with _conn:
        row = _conn.execute(
            "SELECT user_id FROM tiktok_proofs WHERE id=? AND status='pending'", (utid,)
        ).fetchone()
        if not row:
            return None
        _conn.execute(
            "UPDATE tiktok_proofs SET status=? WHERE id=?",
            ("approved" if approve else "rejected", utid),
        )
        if approve:
            _conn.execute(
                "UPDATE users SET balance=balance+? WHERE id=?", (TIKTOK_REWARD_CENTS, row[0])
            )
        return row[0]


def pending_tiktoks(limit: int = 10):
    return _conn.execute(
        "SELECT tp.id, tp.user_id, u.username, u.first_name, tp.proof_file_id "
        "FROM tiktok_proofs tp LEFT JOIN users u ON u.id=tp.user_id "
        "WHERE tp.status='pending' ORDER BY tp.id LIMIT ?",
        (limit,),
    ).fetchall()

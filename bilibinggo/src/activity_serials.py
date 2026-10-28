"""活动序号：每条活动一个唯一短编号，方便你只报「#1372」就能指到具体那条。

- 序号存在 `activity_serials` 表里，**分配后永不变更、永不回收**（活动被删也不复用）；
- 单独建表而不是加列：`activities` 的行会被富化流程整行覆写，加列会被重置成默认值；
- 分配顺序按入库时间（`enriched_at`）先后，所以小号=老活动，大号=新活动，读起来有方向感。
"""
from __future__ import annotations

import time

from sqlalchemy import text
from sqlmodel import select

from src.db.models import ActivityRow, ActivitySerialRow
from src.db.session import session_scope


def assign_with_session(session, dynamic_ids: list[str]) -> dict[str, int]:
    """在同一 session 里补号（不 commit，交给调用方）。已有序号的原样返回。

    取号走 `INSERT ... SELECT MAX(seq)+1` 一条语句：富化是多线程（workers=2）、巡查与面板又是
    两个进程，"先读 max 再插入"在并发下会撞 UNIQUE —— 10-29 03:22 那批 deferred 就是这么来的
    （bind_uid 开始补号之后才暴露）。单语句由 SQLite 的写锁串行化，取号与占号之间没有空隙。
    """
    wanted = [str(x).strip() for x in dynamic_ids if str(x).strip()]
    if not wanted:
        return {}
    existing = {str(row.dynamic_id): int(row.seq) for row in session.exec(select(ActivitySerialRow)).all()}
    missing = [did for did in wanted if did not in existing]
    if missing:
        order = {str(row.dynamic_id): int(row.enriched_at or 0)
                 for row in session.exec(select(ActivityRow).where(ActivityRow.dynamic_id.in_(missing))).all()}
        now = int(time.time())
        for did in sorted(missing, key=lambda x: (order.get(x, 0), x)):
            session.execute(
                text("INSERT INTO activity_serials (dynamic_id, seq, assigned_at, uid) "
                     "SELECT :did, COALESCE(MAX(seq), 0) + 1, :now, 0 FROM activity_serials"),
                {"did": did, "now": now},
            )
        session.flush()
        existing = {str(row.dynamic_id): int(row.seq) for row in session.exec(select(ActivitySerialRow)).all()}
    return {did: existing[did] for did in wanted if did in existing}


def assign(dynamic_ids: list[str]) -> dict[str, int]:
    """给没有序号的行按入库先后补号，返回 {dynamic_id: seq}（已有的原样返回）。"""
    from sqlalchemy.exc import IntegrityError

    last: Exception | None = None
    for _ in range(3):
        try:
            with session_scope() as session:
                out = assign_with_session(session, dynamic_ids)
                session.commit()
                return out
        except IntegrityError as exc:      # 另一个进程刚好占了同一个号：重读 max 再来一次
            last = exc
            time.sleep(0.05)
    raise last  # type: ignore[misc]


def ensure_uid_column() -> None:
    """给已存在的 activity_serials 补 uid 列（幂等，10-27 加的设计）。"""
    from sqlalchemy import text

    from src.db.engine import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        cols = {str(row[1]) for row in conn.execute(text("PRAGMA table_info(activity_serials)")).fetchall()}
        if "uid" not in cols:
            conn.execute(text("ALTER TABLE activity_serials ADD COLUMN uid INTEGER NOT NULL DEFAULT 0"))


def bind_uid(dynamic_id: str, uid: int | str | None) -> None:
    """记录这条活动属于哪个抽奖 UP（uid）。0/空不覆盖已有值。

    富化时序号通常还没分配（面板建视图才补号），只 update 已有行会静默no-op——
    10-27 刚加这个功能时就是这个时序问题，当天之后入库的行 uid 全为 0。
    所以先确保有行，再写 uid。
    """
    try:
        value = int(uid or 0)
    except (TypeError, ValueError):
        return
    did = str(dynamic_id).strip()
    if not value or not did:
        return
    ensure_uid_column()
    assign([did])
    with session_scope() as session:
        row = session.get(ActivitySerialRow, did)
        if row is not None and int(row.uid or 0) != value:
            row.uid = value
            session.commit()


def rows_with_uid() -> list[tuple[str, int, int]]:
    ensure_uid_column()
    with session_scope() as session:
        return [(str(r.dynamic_id), int(r.seq), int(r.uid or 0)) for r in session.exec(select(ActivitySerialRow)).all()]


def by_uid(uid: int) -> list[tuple[str, int, int]]:
    return [row for row in rows_with_uid() if row[2] == int(uid)]


def per_up_index() -> dict[str, int]:
    """同一个 UP 内的第几条（按全局序号先后），给面板显示「UP<uid>·3」。"""
    ensure_uid_column()
    rows = [r for r in rows_with_uid() if r[2]]
    rows.sort(key=lambda r: r[1])
    counters: dict[int, int] = {}
    out: dict[str, int] = {}
    for dynamic_id, _seq, uid in rows:
        counters[uid] = counters.get(uid, 0) + 1
        out[dynamic_id] = counters[uid]
    return out


def backfill_uid_from_guards() -> int:
    """从守卫留痕（follow 步骤 detail=uid=NNN）回填已参加活动的 UP uid，零网络。"""
    import re

    ensure_uid_column()
    with session_scope() as session:
        known = {str(r.dynamic_id): int(r.uid or 0) for r in session.exec(select(ActivitySerialRow)).all()}
    filled = 0
    for dynamic_id, uid in _guard_uid_pairs():
        if uid and known.get(dynamic_id, 0) != uid:
            bind_uid(dynamic_id, uid)
            filled += 1
    return filled


def _guard_uid_pairs() -> list[tuple[str, int]]:
    import json as _json
    import re

    from src.db.models import ParticipationGuardRow

    out: list[tuple[str, int]] = []
    with session_scope() as session:
        for row in session.exec(select(ParticipationGuardRow)).all():
            if not row.dynamic_id:
                continue
            try:
                progress = _json.loads(row.progress_json or "{}")
            except _json.JSONDecodeError:
                continue
            detail = str(((progress.get("follow") or {}).get("detail")) or "")
            match = re.search(r"uid=(\d{4,})", detail)
            if match:
                out.append((str(row.dynamic_id), int(match.group(1))))
    return out


def map_all() -> dict[str, int]:
    with session_scope() as session:
        return {str(row.dynamic_id): int(row.seq) for row in session.exec(select(ActivitySerialRow)).all()}


def reverse_map() -> dict[int, str]:
    return {value: key for key, value in map_all().items()}


def for_id(dynamic_id: str) -> int | None:
    with session_scope() as session:
        row = session.get(ActivitySerialRow, str(dynamic_id).strip())
        return int(row.seq) if row is not None else None


def id_for_seq(seq: int) -> str | None:
    with session_scope() as session:
        row = session.exec(select(ActivitySerialRow).where(ActivitySerialRow.seq == int(seq))).first()
        return str(row.dynamic_id) if row is not None else None


def backfill_all() -> int:
    """给库里所有还没有序号的活动补号（面板首次启用时跑一次）。"""
    with session_scope() as session:
        ids = [str(row.dynamic_id) for row in session.exec(select(ActivityRow)).all()]
    have = map_all()
    missing = [did for did in ids if did not in have]
    if not missing:
        return 0
    assign(missing)
    return len(missing)

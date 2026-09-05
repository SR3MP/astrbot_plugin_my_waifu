"""
本地老婆数据库（每日同步一次，抽取不走 API）
=============================================
痛点：v3.1 每次 /抽老婆 都要实时调 Bangumi API
  （随机作品 -> characters -> detail 逐个请求，一次抽取 2~8 个请求），
  且每年 15 部 × 27 年 ≈ 405 部作品冷启动拉取非常慢。

方案（v3.2）：
  1. 每日同步一次本地库 wife_db.json：候选池 -> 各作品角色 -> 详情
     —— 把所有「符合抽取条件」的角色基础信息存本地，抽取时零 API 请求。
  2. 线索单独缓存 clue_cache.json：抽到某角色首次实时聚合后写缓存，
     之后同角色秒回；每日同步脚本可加 --prefetch-clues 全量预热。
  3. 猜题链路（搜索玩家输入 + 实时对比）保持走 API，不做本地化。

所有可配置数值从 settings 模块读取，可在 WebUI 插件配置页修改。
"""
import json
import os
import random
import signal
import sys
import threading
import time
from datetime import date, timedelta

# 插件以 data.plugins.<目录>.<module> 包路径加载时，插件目录不在 sys.path 顶层
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import settings

from bangumi import (
    CLUE_VERSION,
    _build_year_pool,
    _format_birth,
    get_subject_characters,
    get_character_detail,
    get_cn_name,
    get_aliases,
    parse_infobox,
    subject_type_cn,
    is_wife,
)

DATA_DIR = os.path.join(PLUGIN_DIR, "data")
WIFE_DB = os.path.join(DATA_DIR, "wife_db.json")
CLUE_DB = os.path.join(DATA_DIR, "clue_cache.json")

_MAX_LOG = 8              # 进度日志最多打印条数

_build_lock = threading.Lock()
_clue_lock = threading.Lock()


# ---------- 本地库读写 ----------

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)   # 原子替换：写一半被中断也不会损坏正式文件
    finally:
        # 写入被中断（KeyboardInterrupt/进程被杀）时清掉残留 tmp，避免 stale 文件堆积
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def get_local_db():
    """读取本地库，返回 dict（无则 None）。"""
    return _load_json(WIFE_DB)


def local_db_ok():
    """本地库存在、是今天的、且同步完整（未被中断标记）。"""
    db = _load_json(WIFE_DB)
    return bool(db and db.get("date") == date.today().isoformat()
                and db.get("entries") and not db.get("interrupted"))


def local_db_age_days():
    """本地库距今多少天（无库返回 None）。"""
    db = _load_json(WIFE_DB)
    if not db or not db.get("date"):
        return None
    try:
        d = date.fromisoformat(db["date"])
        return (date.today() - d).days
    except Exception:
        return None


# ---------- 每日同步（构建本地库） ----------

def _wife_entry(c, det, subject):
    """把角色详情转成本地库条目（与 bangumi.random_wife 返回结构对齐）。"""
    info = parse_infobox(det.get("infobox"))
    img = det.get("images") or {}
    actors = [a.get("name") for a in (c.get("actors") or []) if a.get("name")]
    y, mo, d = det.get("birth_year"), det.get("birth_mon"), det.get("birth_day")
    birth = _format_birth(y, mo, d, info)
    return {
        "id": c["id"],
        "name": det.get("name"),
        "name_cn": get_cn_name(info),
        "aliases": get_aliases(info),
        "gender": det.get("gender") or "未知",
        "collects": (det.get("stat") or {}).get("collects", 0),
        "cv": actors[0] if actors else "未知",
        "cvs": actors,
        "age": info.get("年龄", ""),
        "height": info.get("身高", ""),
        "birth": birth,
        "blood": det.get("blood_type") or info.get("血型", ""),
        "summary": (det.get("summary") or "").strip(),
        "image_url": img.get("large") or img.get("medium") or img.get("grid") or "",
        "subject_id": subject.get("id"),
        "subject_name": subject.get("name_cn") or subject.get("name"),
        "subject_type": subject_type_cn(subject.get("type")),
    }


def build_local_db(start_year=None, end_year=None,
                   subject_type=None, per_year=None,
                   min_collects=None, log=print, cancel_flag=None):
    """全量同步：候选池 -> 每部作品角色 -> 详情 -> 写 wife_db.json。

    参数缺省时从 settings 读取配置值。
    返回同步耗时（秒）。中途单点失败跳过，不中断整体。
    """
    if start_year is None:
        start_year = settings.START_YEAR
    if end_year is None:
        end_year = settings.END_YEAR
    if subject_type is None:
        subject_type = settings.SUBJECT_TYPES
    if per_year is None:
        per_year = settings.YEAR_POOL_SIZE
    if min_collects is None:
        min_collects = settings.MIN_COLLECTS

    # 角色类型过滤：仅主角 or 主角+配角
    rels = ["主角"] if settings.MAIN_ONLY else ["主角", "配角"]

    t0 = time.time()
    pool = _build_year_pool(start_year, end_year, subject_type, per_year=per_year)
    log(f"[本地库] 候选池 {len(pool)} 部作品，开始逐部同步角色...")

    entries = {}
    subject_count = 0
    failed = 0
    shown = 0
    interrupted = False
    for i, subject in enumerate(pool, 1):
        if cancel_flag is not None and cancel_flag():
            log("[本地库] 同步被中断")
            interrupted = True
            break
        sid = subject.get("id")
        if not sid:
            continue
        try:
            chars = get_subject_characters(sid)
            time.sleep(settings.SYNC_RATE)
            if isinstance(chars, dict):
                chars = chars.get("data", [])
        except Exception:
            failed += 1
            continue
        mains = [c for c in (chars or []) if c.get("relation") in rels and c.get("id")]
        for c in mains:
            cid = c["id"]
            if cid in entries:
                continue
            try:
                det = get_character_detail(cid)
                time.sleep(settings.SYNC_RATE)
            except Exception:
                failed += 1
                continue
            if det.get("locked"):
                continue
            collects = (det.get("stat") or {}).get("collects", 0)
            if not is_wife(det.get("gender"), collects, min_collects):
                continue
            entries[cid] = _wife_entry(c, det, subject)
        subject_count += 1
        if i % 50 == 0 or i == len(pool):
            log(f"[本地库] 进度 {i}/{len(pool)} 部，已收录 {len(entries)} 位老婆"
                + (f"，失败 {failed}" if failed else ""))

    db = {
        "date": date.today().isoformat(),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(entries),
        "subjects": subject_count,
        "interrupted": interrupted,   # 同步被中断时标记，local_db_ok 视为不新鲜触发重建
        "entries": list(entries.values()),
    }
    _save_json(WIFE_DB, db)
    cost = round(time.time() - t0, 1)
    if interrupted:
        log(f"[本地库] 同步被中断：已保存 {db['count']} 位老婆 / {db['subjects']} 部（部分数据，稍后会重建），耗时 {cost}s")
    else:
        log(f"[本地库] 同步完成：{db['count']} 位老婆 / {db['subjects']} 部作品，耗时 {cost}s")
    return cost


def ensure_local_db_bg():
    """确保本地库新鲜：过期则起后台线程重建（不阻塞玩家）。"""
    if local_db_ok():
        return
    if _build_lock.locked():
        return  # 已在同步中
    th = threading.Thread(target=_sync_worker, daemon=True, name="wife-sync")
    th.start()


def _sync_worker():
    if not _build_lock.acquire(blocking=False):
        return
    try:
        build_local_db(log=lambda m: None)
    finally:
        _build_lock.release()


# ---------- 本地随机抽取（零 API） ----------

def random_wife_local():
    """从本地库随机抽一位老婆。本地库缺失/为空返回 None。"""
    db = _load_json(WIFE_DB)
    entries = (db or {}).get("entries") or []
    if not entries:
        return None
    return random.choice(entries)


# ---------- 线索缓存（猜题对比用，按需聚合一次后缓存） ----------

def get_clue(char_id):
    """从线索缓存取角色聚合信息，无则 None。"""
    db = _load_json(CLUE_DB) or {}
    return db.get(str(char_id))


# 会话字段（写入线索缓存时剥离）。注意：_clue_version 是缓存有效性版本号，
# 必须保留在缓存里，否则读取方无法判断缓存是否过期。
_CLUE_SESSION_FIELDS = frozenset({
    "hint_idx", "_picked_at", "_cost",
})

def cache_clue(char_id, info):
    """写线索缓存（限制条数防止无限膨胀）。

    自动剥离会话相关字段（hint_idx / _picked_at / _cost 等），
    避免上一个用户的猜题进度污染缓存。
    """
    with _clue_lock:
        db = _load_json(CLUE_DB) or {}
        clean = {k: v for k, v in info.items() if k not in _CLUE_SESSION_FIELDS}
        # 显式带上版本号，保证缓存有效性判断始终可用
        clean["_clue_version"] = CLUE_VERSION
        db[str(char_id)] = clean
        keys = list(db.keys())
        if len(keys) > settings.CLUE_CACHE_LIMIT:
            drop_count = max(settings.CLUE_CACHE_LIMIT // 6, 100)
            for k in keys[:drop_count]:
                db.pop(k, None)
        _save_json(CLUE_DB, db)


# ---------- 命令行入口（每日 cron 调用） ----------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日同步本地老婆库")
    ap.add_argument("--prefetch-clues", action="store_true",
                    help="同步后全量预热所有角色的线索缓存（较慢，建议深夜执行）")
    ap.add_argument("--quiet", action="store_true", help="静默模式")
    args = ap.parse_args()

    logf = (lambda m: None) if args.quiet else print

    # Ctrl+C 优雅取消：注册 SIGINT -> 置位事件，build_local_db 循环检测到后
    # 保存已同步部分并标记 interrupted（不会留下「今天的新鲜但残缺」的库）。
    # 修复：原 `threading.Event().is_set` 是临时对象绑定方法，永远返回 False，
    # 取消形同虚设；现用模块级真实 Event + signal handler。
    cancel_evt = threading.Event()

    def _on_sigint(signum, frame):
        logf("[本地库] 收到中断信号，正在保存已同步部分并退出...")
        cancel_evt.set()

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        build_local_db(log=logf, cancel_flag=cancel_evt.is_set)
    except KeyboardInterrupt:
        # 兜底：信号在非主线程/极端时序下仍可能以异常形式到达
        cancel_evt.set()
        logf("[本地库] 被强制中断，已保留原库数据")
        raise SystemExit(130)

    if args.prefetch_clues:
        from bangumi import collect_character_info
        from guess import build_hints
        db = _load_json(WIFE_DB)
        entries = (db or {}).get("entries") or []
        logf(f"[本地库] 线索预热 {len(entries)} 位角色...")
        done, fail = 0, 0
        for i, e in enumerate(entries, 1):
            cid = e.get("id")
            if cid is None or get_clue(cid):
                done += 1
                continue
            try:
                info = collect_character_info(cid)
                info["hints"] = build_hints(info)
                info["aliases"] = e.get("aliases") or []
                info["image_url"] = e.get("image_url")
                cache_clue(cid, info)
                done += 1
            except Exception:
                fail += 1
            time.sleep(settings.SYNC_RATE)
            if i % 50 == 0:
                logf(f"[本地库] 线索预热 {i}/{len(entries)} （成功 {done} 失败 {fail}）")
        logf(f"[本地库] 线索预热完成：{done} 成功 / {fail} 失败")

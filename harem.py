"""
后宫 · 积分系统 数据层（规则 v5 终版）
=====================================
- 每日猜测次数：无限（猜错不耗次数、不扣分）
- 每日给积分次数：可配置（默认 5 次，第 N+1 次起猜对不再给积分）
- 每日迎娶次数：初始可配置（默认 5），可升级（升级花费可配置×已升级次数，递增）
- 后宫容量：初始可配置（默认 1），可扩容（扩容花费可配置×已扩容次数，递增）
- 猜对奖励：按今日第 n 次给分计算，基础奖励 + 梯度加成（均可配置）
- 签到：可配置（默认 +10/天）
- 请回已移除角色：固定花费（可配置，默认 100）

所有数值从 settings 模块读取，可在 WebUI 插件配置页修改。

并发与持久化说明：
- 全部读-改-写在一个模块级 RLock 临界区内执行，避免 AstrBot 多事件并发时
  互相覆盖（丢更新 / 写坏文件）。
- 写盘采用「临时文件 + os.replace」原子替换，进程中断也不会留下半个 JSON。
- 读取时若发现损坏文件，先备份为 .corrupt 再返回空数据，绝不直接覆盖。
"""
import copy
import json
import os
import tempfile
import threading
from datetime import date
from pathlib import Path

import settings

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
PROFILES_FILE = DATA_DIR / "user_profiles.json"

_LOCK = threading.RLock()  # 串行化所有档案读写


def _load():
    if not PROFILES_FILE.exists():
        return {}
    try:
        return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        # 文件损坏：备份现场，绝不用空数据直接覆盖
        try:
            backup = f"{PROFILES_FILE}.corrupt_{int(__import__('time').time())}"
            PROFILES_FILE.rename(backup)
        except Exception:
            pass
        return {}


def _save(profiles):
    DATA_DIR.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PROFILES_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _today():
    return date.today().isoformat()


def _profile(profiles, uid):
    """取用户档案，跨日自动重置每日计数。"""
    p = profiles.setdefault(str(uid), {})
    p.setdefault("points", 0)
    p.setdefault("capacity", settings.CAPACITY_INIT)
    p.setdefault("wed_limit", settings.DAILY_WED_INIT)
    p.setdefault("day", _today())
    p.setdefault("solved_today", 0)   # 今日已给积分的猜对次数
    p.setdefault("wed_today", 0)      # 今日已迎娶次数
    p.setdefault("harem", [])         # [{id,name,wed_at}]
    p.setdefault("removed", [])       # [{id,name,removed_at}]
    if p["day"] != _today():
        p["day"] = _today()
        p["solved_today"] = 0
        p["wed_today"] = 0
    # 清理旧版遗留字段
    p.pop("combo", None)
    p.pop("best_combo", None)
    return p


# ---------- 签到 ----------

def checkin(uid):
    """每日签到，成功 +CHECKIN_REWARD。返回 (ok, points, msg)。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        today = _today()
        if p.get("checkin_day") == today:
            return False, p["points"], "今天已经签过到啦，明天再来~"
        p["checkin_day"] = today
        p["points"] = p.get("points", 0) + settings.CHECKIN_REWARD
        _save(profiles)
        return True, p["points"], f"签到成功 +{settings.CHECKIN_REWARD} 积分，当前 {p['points']}"


# ---------- 猜对结算（梯度积分） ----------

def register_guess(uid, success):
    """
    结算一次猜测。
    - success=True：若今日给积分次数未满，按「今日第 n 次给分」发放梯度奖励
    - success=False：不给积分，不占给积分次数
    返回 dict：{reward, n, solved, reason}（n 为本次给分是今日第几次）
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        if success:
            solved = p["solved_today"] < settings.DAILY_SOLVED_LIMIT
            if solved:
                n = p["solved_today"] + 1          # 本次是今日第 n 次给分
                bonus = settings.SOLVED_BONUS.get(n, 0)
                reward = settings.GUESS_BASE + bonus
                p["points"] = p.get("points", 0) + reward
                p["solved_today"] = n
            else:
                n = p["solved_today"] + 1
                reward = 0
            result = {"reward": reward, "n": n, "solved": solved,
                      "reason": "" if solved else "今日给积分次数已满，本次猜对不再给积分"}
        else:
            result = {"reward": 0, "n": 0, "solved": False,
                      "reason": "猜错了，不给积分"}
        _save(profiles)
        return result


def settle_correct_guess(uid, char):
    """猜对结算：发积分 + 清个人待猜老婆 + 设置待迎娶，一次原子完成。

    三个动作在同一个锁内完成，避免 AstrBot 多事件并发时出现
    「积分已发但 pending 没设上 / 待猜老婆还残留」的中间态。
    char 需含 id 和 name（name_cn 优先）。

    返回 dict：{reward, n, solved, reason}（同 register_guess）
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        solved = p["solved_today"] < settings.DAILY_SOLVED_LIMIT
        if solved:
            n = p["solved_today"] + 1          # 本次是今日第 n 次给分
            bonus = settings.SOLVED_BONUS.get(n, 0)
            reward = settings.GUESS_BASE + bonus
            p["points"] = p.get("points", 0) + reward
            p["solved_today"] = n
        else:
            n = p["solved_today"] + 1
            reward = 0
        # 原子结算：清待猜 → 设待迎娶（同锁内完成）
        p.pop("active_wife", None)
        p["pending_accept"] = {
            "id": char.get("id"),
            "name": char.get("name_cn") or char.get("name"),
        }
        _save(profiles)
        return {"reward": reward, "n": n, "solved": solved,
                "reason": "" if solved else "今日给积分次数已满，本次猜对不再给积分"}


# ---------- 迎娶次数 ----------

def can_wed(uid):
    """今日是否还有迎娶次数。返回 (ok, 剩余次数, 上限)。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        left = p["wed_limit"] - p["wed_today"]
        return left > 0, max(left, 0), p["wed_limit"]


def upgrade_wed_limit(uid):
    """
    升级每日迎娶次数 +1。花费 = WED_UPGRADE_BASE × 已升级次数。
    返回 (ok, msg)。
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        # 兼容旧数据：未记录升级次数时按当前值与初始配置的差值推算
        p.setdefault("wed_upgrades", max(p["wed_limit"] - settings.DAILY_WED_INIT, 0))
        upgrades = p["wed_upgrades"]
        cost = settings.WED_UPGRADE_BASE * (upgrades + 1)
        if p["points"] < cost:
            return False, f"积分不足！升级需 {cost} 积分（当前 {p['points']}）"
        p["points"] -= cost
        p["wed_limit"] += 1
        p["wed_upgrades"] = upgrades + 1
        _save(profiles)
        return True, f"每日迎娶次数 +1（{p['wed_limit']-1} → {p['wed_limit']}），花费 {cost} 积分，剩余 {p['points']}"


# ---------- 后宫容量 ----------

def expand_capacity(uid):
    """
    后宫扩容 +1 格。花费 = CAPACITY_EXPAND_BASE × 已扩容次数。
    返回 (ok, msg)。
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        p.setdefault("capacity_upgrades", max(p["capacity"] - settings.CAPACITY_INIT, 0))
        expansions = p["capacity_upgrades"]
        cost = settings.CAPACITY_EXPAND_BASE * (expansions + 1)
        if p["points"] < cost:
            return False, f"积分不足！扩容需 {cost} 积分（当前 {p['points']}）"
        p["points"] -= cost
        p["capacity"] += 1
        p["capacity_upgrades"] = expansions + 1
        _save(profiles)
        return True, f"后宫扩容 +1 格（{p['capacity']-1} → {p['capacity']}），花费 {cost} 积分，剩余 {p['points']}"


# ---------- 迎娶 / 移除 / 请回 ----------

def add_to_harem(uid, char):
    """
    把角色迎娶进后宫。需：有迎娶次数 + 有容量空格 + 未重复。
    返回 (ok, msg)。
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        cid = str(char["id"])
        if any(str(h["id"]) == cid for h in p["harem"]):
            return False, f"{char['name']} 已经在你的后宫里啦"
        ok, left, limit = can_wed(uid)
        if not ok:
            return False, f"今日迎娶次数已用完（{left} 次），明天再来或先升级每日迎娶次数"
        if len(p["harem"]) >= p["capacity"]:
            return False, f"后宫已满（{len(p['harem'])}/{p['capacity']}），先 /老婆扩容 或用 /老婆移除 腾个位置"
        p["harem"].append({"id": char["id"], "name": char["name"], "wed_at": _today()})
        p["wed_today"] += 1
        _save(profiles)
        return True, f"💕 {char['name']} 已迎娶进后宫！（{len(p['harem'])}/{p['capacity']} 格 · 今日剩 {max(limit-1,0)} 次）"


def remove_from_harem(uid, key):
    """
    移除后宫成员。key 可以是名字（含别名子串）或角色 id。
    移除后记入 removed（请回需花积分）。
    返回 (ok, msg)。
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        key = str(key).strip().lower()
        idx = -1
        for i, h in enumerate(p["harem"]):
            if str(h["id"]) == key or key in str(h["name"]).lower():
                idx = i
                break
        if idx < 0:
            return False, "后宫没有这位，用 /老婆后宫 看看都有谁吧"
        h = p["harem"].pop(idx)
        p["removed"].append({"id": h["id"], "name": h["name"], "removed_at": _today()})
        _save(profiles)
        return True, f"已将 {h['name']} 移除后宫。之后想 /老婆请回 她的话，要花 {settings.REINVITE_COST} 积分哦"


def reinvite(uid, key):
    """
    请回已移除的角色：需积分 + 有容量空格。花费 REINVITE_COST 固定。
    返回 (ok, msg)。
    """
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        key = str(key).strip().lower()
        idx = -1
        for i, r in enumerate(p["removed"]):
            if str(r["id"]) == key or key in str(r["name"]).lower():
                idx = i
                break
        if idx < 0:
            return False, "已移除名单里没有这位"
        if p["points"] < settings.REINVITE_COST:
            return False, f"请回需 {settings.REINVITE_COST} 积分，当前只有 {p['points']}"
        if len(p["harem"]) >= p["capacity"]:
            return False, f"后宫已满（{len(p['harem'])}/{p['capacity']}），先 /老婆扩容 或用 /老婆移除 腾个位置"
        r = p["removed"].pop(idx)
        p["points"] -= settings.REINVITE_COST
        p["harem"].append({"id": r["id"], "name": r["name"], "wed_at": _today()})
        _save(profiles)
        return True, f"💕 {r['name']} 已花 {settings.REINVITE_COST} 积分请回后宫！（剩余 {p['points']} 积分）"


# ---------- 个人待猜老婆（每人一份，猜中入宫） ----------

def get_active_wife(uid):
    """取用户当前待猜的神秘老婆（dict），没有返回 None。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        return p.get("active_wife")


def set_active_wife(uid, wife):
    """设置用户当前待猜的神秘老婆。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        if wife is None:
            p.pop("active_wife", None)
        else:
            p["active_wife"] = wife
        _save(profiles)


def clear_active_wife(uid):
    """清空用户待猜老婆（猜中/放弃后调用）。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        p.pop("active_wife", None)
        _save(profiles)


def get_pending_accept(uid):
    """取用户已猜中、待迎娶的老婆 dict，没有返回 None。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        return p.get("pending_accept")


def set_pending_accept(uid, wife):
    """设置用户待迎娶的老婆。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        if wife is None:
            p.pop("pending_accept", None)
        else:
            p["pending_accept"] = wife
        _save(profiles)


# ---------- 查询 ----------

def profile_view(uid):
    """返回用户档案展示所需信息。"""
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        _, wed_left, wed_limit = can_wed(uid)
        return {
            "points": p["points"],
            "capacity": p["capacity"],
            "harem": list(p["harem"]),
            "removed": list(p["removed"]),
            "wed_limit": wed_limit,
            "wed_left": wed_left,
            "solved_today": p["solved_today"],
            "solved_left": max(settings.DAILY_SOLVED_LIMIT - p["solved_today"], 0),
        }


def remember_name(uid, name):
    """记录/更新用户昵称（用于群榜展示，避免只显示 openid/uid）。
    仅在名字非空且与 uid 不同时写入。"""
    if not uid or not name or str(name).strip() == str(uid):
        return
    name = str(name).strip()
    with _LOCK:
        profiles = _load()
        p = _profile(profiles, uid)
        if p.get("display_name") != name:
            p["display_name"] = name
            _save(profiles)


def get_all_profiles():
    """返回所有用户档案字典（uid -> profile），用于群聊全局查询。
    已调用的 _profile 会按当天重置每日计数，避免返回过期数据。
    返回的是真深拷贝（copy.deepcopy），外部修改不会污染内存中的源数据。
    """
    with _LOCK:
        profiles = _load()
        for uid in list(profiles.keys()):
            _profile(profiles, uid)
        # 真深拷贝：嵌套的 harem/removed/pending 等结构也一并隔离
        return copy.deepcopy(profiles)

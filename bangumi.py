"""
Bangumi v0 API 封装层
=====================
按年份+热度排序抽作品，作品池 = 每年热度前 N 部动画

与 v2.1 的关键区别（v2.1 遗留问题：作品池硬编码只有 2 部）：
    v2.1: SUBJECT_POOL 硬编码 2 个 subject id -> 候选 7 个（小圆占 6/7）
    v3.0: 按年份+热度排序抽作品，作品池 = 每年热度前 N 部动画
          （2000~2026 逐年搜索，跨年去重后约 400 部候选，当天磁盘缓存复用）

所有可配置数值从 settings 模块读取，可在 WebUI 插件配置页修改。
"""
import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime

import settings

API = "https://api.bgm.tv/v0"
# Bangumi API 要求 UA 带应用名+联系方式（官网规范：AppName/Version (contact)）
UA = {"User-Agent": "astrbot-plugin-my-waifu/3.2.0 (https://github.com/SR3MP/astrbot_plugin_my_waifu)"}
SEARCH_LIMIT_MAX = 25   # v0 search 接口 limit 的保守上限，超过会被服务端拒绝
BATCH_SIZE = 10         # search 每次批量取的条数（API 技术参数，不可配置）

CLUE_VERSION = 4    # 线索/评分聚合格式版本；v3.2 起类型过滤跟随 settings.SUBJECT_TYPES（默认 [2] 动画）

# 猜题用的线索聚合（guessr getCharacterAppearances 的 sourceTagMap）
SOURCE_TAG_MAP = {
    "GAL改": "游戏改",
    "轻小说改": "小说改",
    "轻改": "小说改",
    "网文改": "小说改",
    "漫改": "漫画改",
    "漫画改编": "漫画改",
    "游戏改编": "游戏改",
    "小说改编": "小说改",
    "原创动画": "原创",
}


# ---------- 基础 HTTP ----------

def _urlopen(req):
    """带重试的请求：指数退避，网络波动/限流自动重试。

    超时只依赖 urlopen 的 timeout 参数（作用于连接+读取），
    不再触碰 socket.setdefaulttimeout —— 那是进程级全局状态，
    多线程并发设置/恢复会互相覆盖（A 线程设置的超时会被 B 线程
    恢复掉），是历史版本的竞态隐患。TLS 握手阶段 socket 已由
    urlopen 创建并带 timeout，同样受控。
    """
    last = None
    for attempt in range(settings.RETRY_TIMES + 1):
        try:
            with urllib.request.urlopen(req, timeout=settings.HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                socket.timeout, TimeoutError, ConnectionError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                break  # 非限流/非5xx错误不重试
            if attempt < settings.RETRY_TIMES:
                time.sleep(settings.RETRY_BASE * (2 ** attempt))
    raise last


def _get(path):
    """GET Bangumi v0 API，返回解析后的 JSON。"""
    req = urllib.request.Request(f"{API}{path}", headers=UA)
    return _urlopen(req)


def _post(path, body):
    """POST Bangumi v0 API（search 系列接口是 POST）。"""
    data = json.dumps(body).encode("utf-8")
    headers = {**UA, "Content-Type": "application/json"}
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method="POST")
    return _urlopen(req)


# ---------- 单点接口 ----------

def search_subjects(subject_type, start_year, end_year, limit=BATCH_SIZE, offset=0):
    """按热度排序搜索作品（guessr buildFilter + fetchSubjects 的对应实现）。

    subject_type: [2] / [1,2,4,6] 等 Bangumi 大类
    日期上界用当前时间截断，避免抽到尚未播出的作品（与 guessr 一致）。
    """
    today = datetime.now()
    end_date = min(datetime(end_year + 1, 1, 1), today).strftime("%Y-%m-%d")
    body = {
        "sort": "heat",
        "filter": {
            "type": subject_type,
            "air_date": [f">={start_year}-01-01", f"<{end_date}"],
        },
    }
    return _post(f"/search/subjects?limit={limit}&offset={offset}", body)


def get_subject_characters(subject_id):
    """GET /v0/subjects/{id}/characters"""
    return _get(f"/subjects/{subject_id}/characters")


def get_character_detail(character_id):
    """GET /v0/characters/{id}"""
    return _get(f"/characters/{character_id}")


def get_character_subjects(character_id):
    """GET /v0/characters/{id}/subjects"""
    return _get(f"/characters/{character_id}/subjects")


def get_subject_detail(subject_id):
    """GET /v0/subjects/{id}（含 air_date / rating.score / meta_tags / tags，供线索补全）"""
    return _get(f"/subjects/{subject_id}")


def get_character_persons(character_id):
    """GET /v0/characters/{id}/persons（CV）"""
    return _get(f"/characters/{character_id}/persons")


def search_characters(keyword, limit=10, offset=0):
    """POST /v0/search/characters，按名字搜角色（猜题用，与 guessr SearchBar 一致）。

    limit 钳制在 1~SEARCH_LIMIT_MAX，避免超过 API 上限被服务端拒绝；
    main.py 猜题时传 16 条正是为了覆盖"前缀路人占位"场景。
    """
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    return _post(f"/search/characters?limit={limit}&offset={offset}",
                 {"keyword": keyword.strip()})


# ---------- 工具 ----------

def _extract_aliases_value(v):
    """从 infobox 别名字段提取字符串列表（兼容 dict/list/str/嵌套结构）。

    Bangumi 别名字段常见形态：
      - [{"v": "日文名"}, {"v": "罗马字"}]       （最常见）
      - ["纯字符串别名", ...]
      - {"k": "别名", "v": "..."} / 单字符串
    统一收敛成去重保序的字符串列表。
    """
    out = []
    if isinstance(v, dict):
        if v.get("v") is not None:
            out.append(str(v["v"]).strip())
        else:
            for sub in v.values():
                out.extend(_extract_aliases_value(sub))
    elif isinstance(v, (list, tuple)):
        for x in v:
            out.extend(_extract_aliases_value(x))
    elif v is not None:
        s = str(v).strip()
        if s:
            out.append(s)
    seen, result = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result


def parse_infobox(infobox):
    """把 Bangumi infobox（[{key,value},...]）摊平成 dict。

    「别名」字段结构特殊（常为 [{"v": ...}, ...]），
    若按普通 list 压平会丢失结构，这里保留为字符串列表。
    """
    out = {}
    for item in infobox or []:
        k, v = item.get("key"), item.get("value")
        if not k or v is None:
            continue
        if k == "别名":
            out[k] = _extract_aliases_value(v)
            continue
        if isinstance(v, list):
            v = " / ".join(str(x) for x in v)
        out[k] = str(v)
    return out


def _format_birth(year, month, day, info):
    """把 birth_year/month/day 拼成合法日期串；字段缺失/非法时回退 infobox 生日。"""
    if not year:
        return info.get("生日", "")
    try:
        y = int(year)
        mo = int(month or 0)
        d = int(day or 0)
    except (ValueError, TypeError):
        return info.get("生日", "")
    if not (1 <= mo <= 12) or not (1 <= d <= 31):
        # 不完整的出生信息：只保留年份，避免生成 "2020-00-00" 这类非法日期
        return str(y)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def get_cn_name(info):
    """从 infobox 提取简体中文名。"""
    return info.get("简体中文名") or info.get("中文译名") or info.get("中文名称") or ""


def get_aliases(info):
    """从 infobox「别名」字段提取别名列表（含英文名/罗马字）。

    兼容 parse_infobox 处理后的 list 形态，以及原始字符串形态。
    """
    aliases = info.get("别名")
    if isinstance(aliases, list):
        out = []
        for a in aliases:
            if isinstance(a, dict):
                out.append(str(a.get("v", "")).strip())
            elif isinstance(a, str):
                out.append(a.strip())
            elif isinstance(a, (int, float)):
                out.append(str(a))
        return [x for x in out if x]
    if isinstance(aliases, str) and aliases.strip():
        return [aliases.strip()]
    return []


def year_of_subject(subject):
    """从出演作品条目里取年份（air_date / date 字段，格式 YYYY-MM-DD）。"""
    date = subject.get("air_date") or subject.get("date") or ""
    if isinstance(date, (list, tuple)) and date:
        date = date[0]
    return int(str(date)[:4]) if len(str(date)) >= 4 and str(date)[:4].isdigit() else None


def subject_name(subject):
    """作品名（优先中文名）。"""
    return subject.get("name_cn") or subject.get("name") or ""


def subject_type_cn(t):
    return {1: "书籍", 2: "动画", 4: "游戏", 6: "三次元", 8: "其他"}.get(t, "其他")


# ---------- 老婆抽取（guessr getRandomCharacter 的简化版） ----------


def is_wife(gender, collects, min_collects):
    """过滤规则（沿用 v2.1 用户拍板：女+无性别、收藏数下限、非锁定）。"""
    if gender == "male":
        return False
    if collects <= min_collects:
        return False
    return True


# 候选池缓存（内存 + 磁盘双缓存，按日期失效，一天只构建一次）
_POOL_CACHE = {}
_POOL_BUILD_LOCK = threading.Lock()


def _pool_cache_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".year_pool_cache.json")


def _build_year_pool(start_year=None, end_year=None,
                     subject_type=None, per_year=None):
    """遍历每一年，各取当年热度前 per_year 部作品组成候选池。

    每年一次 search：type + 当年 air_date 区间 + 按热度排序，取前 per_year 条。
    结果按 id 去重合并。首次构建会遍历所有年份（约 27 个请求，较慢），
    结果落到磁盘按日期缓存，当天再次调用秒回。

    参数缺省时从 settings 读取配置值。
    """
    if start_year is None:
        start_year = settings.START_YEAR
    if end_year is None:
        end_year = settings.END_YEAR
    if subject_type is None:
        subject_type = settings.SUBJECT_TYPES
    if per_year is None:
        per_year = settings.YEAR_POOL_SIZE

    today = date.today().isoformat()
    key = (today, tuple(subject_type), start_year, end_year, per_year)

    # 1) 内存缓存
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]

    with _POOL_BUILD_LOCK:
        # 双检锁：拿到锁后再确认一次缓存，避免预热与抽取并发重复构建
        if key in _POOL_CACHE:
            return _POOL_CACHE[key]

        # 2) 磁盘缓存（当天有效）
        try:
            with open(_pool_cache_path(), encoding="utf-8") as f:
                data = json.load(f)
            if (data.get("date") == today
                    and tuple(data.get("types", [])) == tuple(subject_type)
                    and data.get("start") == start_year
                    and data.get("end") == end_year
                    and data.get("per") == per_year):
                items = data.get("items") or []
                _POOL_CACHE[key] = items
                return items
        except Exception:
            pass

        # 3) 构建候选池
        pool = {}
        for y in range(start_year, end_year + 1):
            try:
                batch = search_subjects(subject_type, y, y, limit=per_year, offset=0)
                time.sleep(settings.RATE_LIMIT)
                for s in (batch.get("data") or []):
                    sid = s.get("id")
                    if sid and sid not in pool:
                        pool[sid] = s
            except Exception:
                continue
        items = list(pool.values())

        # 4) 写磁盘缓存
        try:
            with open(_pool_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"date": today, "types": list(subject_type),
                           "start": start_year, "end": end_year, "per": per_year,
                           "items": items}, f, ensure_ascii=False)
        except Exception:
            pass

        _POOL_CACHE[key] = items
        return items


def random_wife(start_year=None, end_year=None,
                subject_type=None, main_only=None,
                min_collects=None, max_attempts=None):
    """实时抽一位老婆。

    参数缺省时从 settings 读取配置值。
    """
    if start_year is None:
        start_year = settings.START_YEAR
    if end_year is None:
        end_year = settings.END_YEAR
    if subject_type is None:
        subject_type = settings.SUBJECT_TYPES
    if main_only is None:
        main_only = settings.MAIN_ONLY
    if min_collects is None:
        min_collects = settings.MIN_COLLECTS
    if max_attempts is None:
        max_attempts = settings.MAX_ATTEMPTS

    rels = ["主角"] if main_only else ["主角", "配角"]
    pool = _build_year_pool(start_year, end_year, subject_type)
    if not pool:
        return None
    for _ in range(max_attempts):
        try:
            subject = random.choice(pool)
            sid = subject.get("id")
            if not sid:
                continue

            # 步骤2：作品 -> 角色
            chars = get_subject_characters(sid)
            time.sleep(settings.RATE_LIMIT)
            if isinstance(chars, dict):
                chars = chars.get("data", [])
            mains = [c for c in (chars or []) if c.get("relation") in rels and c.get("id")]
            if not mains:
                continue

            random.shuffle(mains)
            for c in mains:
                try:
                    det = get_character_detail(c["id"])
                    time.sleep(settings.RATE_LIMIT)
                except Exception:
                    continue
                if det.get("locked"):
                    continue
                collects = (det.get("stat") or {}).get("collects", 0)
                if not is_wife(det.get("gender"), collects, min_collects):
                    continue

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
                    "collects": collects,
                    "cv": actors[0] if actors else "未知",
                    "cvs": actors,
                    "age": info.get("年龄", ""),
                    "height": info.get("身高", ""),
                    "birth": birth,
                    "blood": det.get("blood_type") or info.get("血型", ""),
                    "summary": (det.get("summary") or "").strip(),
                    "image_url": img.get("large") or img.get("medium") or img.get("grid") or "",
                    "subject_id": sid,
                    "subject_name": subject.get("name_cn") or subject.get("name"),
                    "subject_type": subject_type_cn(subject.get("type")),
                }
        except Exception as e:
            # 网络波动：跳过本次尝试，继续重试
            time.sleep(settings.RATE_LIMIT)
    return None


# ---------- 猜题线索聚合（guessr getCharacterAppearances 的简化版） ----------

def collect_character_info(character_id):
    """聚合一个角色的全部线索：出演作品/年代范围/最高评分/CV/性别/别名/简介。

    对应 guessr getCharacterAppearances 的核心聚合逻辑（这里保留猜题必需维度）。
    用于「答案角色」和「玩家猜的角色」两端，之后做集合对比。
    """
    det = get_character_detail(character_id)
    time.sleep(settings.RATE_LIMIT)
    info = parse_infobox(det.get("infobox"))
    img = det.get("images") or {}

    subs = get_character_subjects(character_id)
    time.sleep(settings.RATE_LIMIT)
    if isinstance(subs, dict):
        subs = subs.get("data", [])

    persons = get_character_persons(character_id)
    time.sleep(settings.RATE_LIMIT)
    if isinstance(persons, dict):
        persons = persons.get("data", [])

    # /subjects 接口不含 air_date/score/meta_tags，需逐个补查作品详情
    # 为避免请求爆炸，对作品去重并限制补查数量
    # 只统计抽取池配置的作品类型（SUBJECT_TYPES，默认仅动画），排除其他类型
    detail_cache = {}
    for s in subs or []:
        if s.get("type") not in settings.SUBJECT_TYPES:
            continue
        if s.get("staff") not in ("主角", "配角"):
            continue
        sid = s.get("id")
        if not sid or sid in detail_cache:
            continue
        try:
            det_s = get_subject_detail(sid)
            time.sleep(settings.RATE_LIMIT)
            detail_cache[sid] = det_s
        except Exception:
            detail_cache[sid] = None
        if len(detail_cache) >= settings.DETAIL_FETCH_LIMIT:
            break

    works = []
    for s in subs or []:
        # 只统计抽取池配置的作品类型（SUBJECT_TYPES），排除其他类型
        if s.get("type") not in settings.SUBJECT_TYPES:
            continue
        # guessr 只统计 主角/配角
        if s.get("staff") not in ("主角", "配角"):
            continue
        sid = s.get("id")
        det_s = detail_cache.get(sid) if sid else None
        if det_s:
            # 详情接口里的 air_date / rating.score / meta_tags
            y = year_of_subject(det_s)
            score = ((det_s.get("rating") or {}).get("score")) or 0
            meta = det_s.get("meta_tags") or []
        else:
            y = year_of_subject(s)
            score = s.get("score") or 0
            meta = []
        # 未播出作品（无 air_date）在 Bangumi 上会有默认的"期待值"评分（如 10 分），
        # 不算真实评分，一律归零，避免污染最高评分/提示线索
        if y is None:
            score = 0
        works.append({
            "id": sid,
            "name": subject_name(s),
            "year": y,
            "type": subject_type_cn(s.get("type")),
            "staff": s.get("staff"),
            "score": score,
            "meta_tags": meta,
        })
    works.sort(key=lambda w: (w["year"] or 0), reverse=True)

    years = [w["year"] for w in works if w["year"]]
    cvs = []
    for p in persons or []:
        n = p.get("name")
        if n and n not in cvs:
            cvs.append(n)

    # 聚合 tag：来源标签（meta_tags）归一到 SOURCE_TAG_MAP
    tags = []
    seen_tags = set()
    for w in works:
        for mt in w.get("meta_tags") or []:
            t = SOURCE_TAG_MAP.get(mt, mt)
            if t and t not in seen_tags:
                seen_tags.add(t)
                tags.append(t)
    tags.sort()

    collect_tags = {
        "id": character_id,
        "name": det.get("name"),
        "name_cn": get_cn_name(info),
        "aliases": get_aliases(info),
        "gender": det.get("gender") or "未知",
        "collects": (det.get("stat") or {}).get("collects", 0),
        "summary": (det.get("summary") or "").strip(),
        "image_url": img.get("large") or img.get("medium") or img.get("grid") or "",
        "works": works,
        "work_names": [w["name"] for w in works],
        "years": years,
        "latest_year": max(years) if years else None,
        "earliest_year": min(years) if years else None,
        "highest_rating": max([w["score"] for w in works if w["score"] and w["year"]] or [0]),
        "cvs": cvs,
        "tags": tags,
        "_clue_version": CLUE_VERSION,   # 评分聚合格式版本，旧缓存自动作废
    }
    return collect_tags

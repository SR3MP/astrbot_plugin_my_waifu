"""
插件配置中心
============
所有可配置数值集中在此，默认值与原硬编码值一致。
WifePlugin.__init__ 在 AstrBot 传入 config 后调用 load() 覆盖默认值。

各模块（harem / bangumi / local_db / guess）统一从这里取值，
不再各自定义硬编码常量。
"""

# ===== 游戏数值 =====
CHECKIN_REWARD = 10
GUESS_BASE = 20
SOLVED_BONUS = {1: 0, 2: 5, 3: 10, 4: 15, 5: 20}
DAILY_SOLVED_LIMIT = 5
DAILY_WED_INIT = 5
WED_UPGRADE_BASE = 30
CAPACITY_INIT = 1
CAPACITY_EXPAND_BASE = 50
REINVITE_COST = 100

# ===== 抽取设定 =====
MIN_COLLECTS = 450
START_YEAR = 2000
END_YEAR = 2026
YEAR_POOL_SIZE = 15
SUBJECT_TYPES = [2]
MAIN_ONLY = True
MAX_ATTEMPTS = 10
HINTS_TOTAL = 6

# ===== 网络与API =====
HTTP_TIMEOUT = 6          # 单次请求超时（秒），宜小不宜大，避免阻塞事件循环
IMAGE_HTTP_TIMEOUT = 12
RATE_LIMIT = 0.15
DETAIL_FETCH_LIMIT = 40
RETRY_TIMES = 1           # 重试次数：网络差时多次重试会放大阻塞时间
RETRY_BASE = 1.0
GUESS_SEARCH_LIMIT = 16   # /老婆猜 搜索返回条数上限（原硬编码 16）
DRAW_TIMEOUT = 90         # /抽老婆 整条链路总超时（秒）：超时直接回"网络波动"，不让玩家无限等
POOL_LOCK_TIMEOUT = 10    # 建池锁等待超时（秒）：有人在建池（如后台同步）时抽取线程不无限阻塞
POOL_EMPTY_BACKOFF = 300  # 空池冷却（秒）：建池结果为空的短时间内快速失败，避免反复全量重建轰炸 API

# ===== API 数据源 =====
API_SOURCE = "official"   # "official"=官方直连(api.bgm.tv) / "mirror"=境内镜像(bgmapi.anibt.net) / "https://..." 自定义反代URL

_API_BASES = {
    "official": "https://api.bgm.tv/v0",
    "mirror":   "https://bgmapi.anibt.net/v0",
}


def api_base():
    """返回当前 API 基地址 URL（带 /v0 后缀）。

    支持三种值：
      - "official" -> 官方 api.bgm.tv
      - "mirror"   -> 境内镜像 bgmapi.anibt.net（自动改写响应中的图片 URL 为 bgmimg.anibt.net）
      - "https://..." -> 自定义反代 URL（自动补 /v0 后缀）
    """
    src = (API_SOURCE or "official").strip()
    if src in _API_BASES:
        return _API_BASES[src]
    if src.startswith("http"):
        base = src.rstrip("/")
        return base if base.endswith("/v0") else base + "/v0"
    return _API_BASES["official"]


# ===== 本地库 =====
SYNC_RATE = 0.1
CLUE_CACHE_LIMIT = 3000
SYNC_BACKOFF = 600        # 同步冷却（秒）：上一次同步尝试失败/产出空库后，N 秒内不重复触发重建

# ===== 卡池浏览 =====
POOL_PAGE_SIZE = 50       # /老婆卡池 <页码> 每页显示的角色数（立绘墙一页的人头数）


def _as_int(value, default, lo=None, hi=None):
    """把配置值安全转 int；非法值回退默认；lo/hi 非空时钳制范围。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _as_float(value, default, lo=None, hi=None):
    """把配置值安全转 float；非法值回退默认；lo/hi 非空时钳制范围。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def load(config):
    """从 AstrBotConfig dict 覆盖默认值。config 为 None 或空时保持默认。

    所有数值转换都走 _as_int/_as_float 容错：WebUI 配置里某个字段填了
    非法字符串（如 "abc"）不会让插件加载失败，而是回退默认值。
    """
    if not config:
        return

    g = config.get("game") or {}
    global CHECKIN_REWARD, GUESS_BASE, SOLVED_BONUS, DAILY_SOLVED_LIMIT
    global DAILY_WED_INIT, WED_UPGRADE_BASE, CAPACITY_INIT, CAPACITY_EXPAND_BASE, REINVITE_COST
    CHECKIN_REWARD = _as_int(g.get("checkin_reward"), CHECKIN_REWARD)
    GUESS_BASE = _as_int(g.get("guess_base"), GUESS_BASE)
    bonus_list = g.get("solved_bonus")
    if isinstance(bonus_list, list) and bonus_list:
        cleaned = {}
        for i, v in enumerate(bonus_list):
            iv = _as_int(v, 0)
            cleaned[i + 1] = iv
        if cleaned:
            SOLVED_BONUS = cleaned
    DAILY_SOLVED_LIMIT = _as_int(g.get("daily_solved_limit"), DAILY_SOLVED_LIMIT)
    DAILY_WED_INIT = _as_int(g.get("daily_wed_init"), DAILY_WED_INIT)
    WED_UPGRADE_BASE = _as_int(g.get("wed_upgrade_base"), WED_UPGRADE_BASE)
    CAPACITY_INIT = _as_int(g.get("capacity_init"), CAPACITY_INIT)
    CAPACITY_EXPAND_BASE = _as_int(g.get("capacity_expand_base"), CAPACITY_EXPAND_BASE)
    REINVITE_COST = _as_int(g.get("reinvite_cost"), REINVITE_COST)

    e = config.get("extraction") or {}
    global MIN_COLLECTS, START_YEAR, END_YEAR, YEAR_POOL_SIZE
    global SUBJECT_TYPES, MAIN_ONLY, MAX_ATTEMPTS, HINTS_TOTAL
    MIN_COLLECTS = _as_int(e.get("min_collects"), MIN_COLLECTS)
    START_YEAR = _as_int(e.get("start_year"), START_YEAR)
    END_YEAR = _as_int(e.get("end_year"), END_YEAR)
    YEAR_POOL_SIZE = _as_int(e.get("year_pool_size"), YEAR_POOL_SIZE)
    types = e.get("subject_types")
    if isinstance(types, list) and types:
        SUBJECT_TYPES = []
        for t in types:
            try:
                SUBJECT_TYPES.append(int(t))
            except (TypeError, ValueError):
                continue
        if not SUBJECT_TYPES:
            SUBJECT_TYPES = [2]
    main_only = e.get("main_only")
    if isinstance(main_only, bool):
        MAIN_ONLY = main_only
    elif isinstance(main_only, str):
        MAIN_ONLY = main_only.strip().lower() in ("1", "true", "yes", "on")
    MAX_ATTEMPTS = _as_int(e.get("max_attempts"), MAX_ATTEMPTS)
    HINTS_TOTAL = _as_int(e.get("hints_total"), HINTS_TOTAL, lo=1, hi=8)   # 钳制在 1~8 条

    n = config.get("network") or {}
    global HTTP_TIMEOUT, IMAGE_HTTP_TIMEOUT, RATE_LIMIT, DETAIL_FETCH_LIMIT, RETRY_TIMES, RETRY_BASE
    global GUESS_SEARCH_LIMIT, DRAW_TIMEOUT, POOL_LOCK_TIMEOUT, POOL_EMPTY_BACKOFF
    HTTP_TIMEOUT = _as_int(n.get("http_timeout"), HTTP_TIMEOUT, lo=1, hi=10)   # 钳制在 1~10s
    IMAGE_HTTP_TIMEOUT = _as_int(n.get("image_http_timeout"), IMAGE_HTTP_TIMEOUT, lo=1, hi=20)
    RATE_LIMIT = _as_float(n.get("rate_limit"), RATE_LIMIT, lo=0.0)
    DETAIL_FETCH_LIMIT = _as_int(n.get("detail_fetch_limit"), DETAIL_FETCH_LIMIT, lo=1)
    RETRY_TIMES = _as_int(n.get("retry_times"), RETRY_TIMES, lo=0, hi=2)       # 钳制在 0~2 次
    RETRY_BASE = _as_float(n.get("retry_base"), RETRY_BASE, lo=0.2, hi=3)   # 钳制在 0.2~3s
    GUESS_SEARCH_LIMIT = _as_int(n.get("guess_search_limit"), GUESS_SEARCH_LIMIT, lo=1, hi=25)  # 钳制在 1~25（API 上限）
    DRAW_TIMEOUT = _as_int(n.get("draw_timeout"), DRAW_TIMEOUT, lo=10, hi=300)   # 钳制在 10~300s
    POOL_LOCK_TIMEOUT = _as_int(n.get("pool_lock_timeout"), POOL_LOCK_TIMEOUT, lo=1, hi=60)
    POOL_EMPTY_BACKOFF = _as_int(n.get("pool_empty_backoff"), POOL_EMPTY_BACKOFF, lo=0)

    global API_SOURCE
    api_src = n.get("api_source")
    if isinstance(api_src, str) and api_src.strip():
        API_SOURCE = api_src.strip()

    ldb = config.get("local_db") or {}
    global SYNC_RATE, CLUE_CACHE_LIMIT, SYNC_BACKOFF
    SYNC_RATE = _as_float(ldb.get("sync_rate"), SYNC_RATE, lo=0.0)
    CLUE_CACHE_LIMIT = _as_int(ldb.get("clue_cache_limit"), CLUE_CACHE_LIMIT, lo=1)
    SYNC_BACKOFF = _as_int(ldb.get("sync_backoff"), SYNC_BACKOFF, lo=0)

    pl = config.get("pool") or {}
    global POOL_PAGE_SIZE
    POOL_PAGE_SIZE = _as_int(pl.get("page_size"), POOL_PAGE_SIZE, lo=10, hi=200)  # 钳制在 10~200 人/页

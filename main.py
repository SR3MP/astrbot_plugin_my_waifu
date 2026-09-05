"""
专属老婆插件 v3.2（Bangumi 实时版 + 后宫积分系统 + 全配置化）
==============================================
- v2.1 遗留问题：SUBJECT_POOL 硬编码仅 2 部作品
- v3.0 解决：用 POST /v0/search/subjects 按年份+热度排序随机抽作品，
  作品池 = Bangumi 全站数万部，无需再人工硬编码 ID
- v3.1：移除全群共享的「今日老婆 / 今日猜题」，仅保留个人专属老婆养成
- v3.2：本地库每日同步 + 全配置化（_conf_schema.json + settings.py）
- 后宫系统：/抽老婆 抽专属老婆→/老婆猜 猜中→/老婆迎娶 迎娶进后宫；
  签到/老婆猜对赚积分，扩容/老婆升迎娶/请回花积分

所有数值/设定均可在 WebUI 插件配置页修改，无需改代码。

功能命令：
    /抽老婆              抽一位只属于自己的神秘老婆，猜对可迎娶
    /老婆猜 <名字>           提交猜测，猜错自动解锁下一条线索
    /老婆迎娶                把刚猜对的老婆迎娶进后宫
    /老婆放弃                放弃正在猜的老婆，重新抽
    /老婆签到                每日签到领积分
    /老婆后宫                查看后宫、积分与冷宫名单
    /老婆扩容                花积分扩充后宫容量
    /老婆升迎娶              花积分提升每日迎娶次数
    /老婆移除 <名字>         把后宫里的老婆移除（打入冷宫）
    /老婆请回 <名字>         花积分把打入冷宫的老婆请回后宫
    /老婆群榜                查看全群老婆现状排行榜
    /老婆卡池                查看卡池/查角色/按作品查
    /老婆立绘 <名字>         查看角色立绘（无图自动下载）
    /老婆帮助            查看帮助与游戏规则
"""
import sys
import time
import threading
import asyncio
import zhconv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# 插件以 data.plugins.<目录>.<module> 包路径加载时，插件目录不在 sys.path 顶层，
# 必须手动加入，否则 from bangumi import ... 会报 ModuleNotFoundError。
PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# 防止 AstrBot 长期运行后 sys.modules 缓存旧版插件模块对象（热重载不清缓存，
# 会出现"文件里明明有 CLUE_VERSION 却 cannot import name"）。强制重新加载磁盘最新版。
for _m in (
    "bangumi",
    "settings",
    "guess",
    "harem",
    "local_db",
    "data.plugins.astrbot_plugin_today_wife.bangumi",
    "data.plugins.astrbot_plugin_today_wife.settings",
    "data.plugins.astrbot_plugin_today_wife.guess",
    "data.plugins.astrbot_plugin_today_wife.harem",
    "data.plugins.astrbot_plugin_today_wife.local_db",
):
    sys.modules.pop(_m, None)

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

import settings
from bangumi import (
    random_wife,
    collect_character_info,
    search_characters,
    get_character_detail,
    parse_infobox,
    get_cn_name,
    CLUE_VERSION,
)
from guess import compare, build_hints
import harem
import local_db

DATA_DIR = PLUGIN_DIR / "data"
IMAGE_DIR = PLUGIN_DIR / "images"
UA = {"User-Agent": "astrbot-plugin-my-waifu/3.2.0 (https://github.com/SR3MP/astrbot_plugin_my_waifu)"}

# 猜题匹配：搜索结果的收藏数缓存（跨猜题复用；进程内，热重载/重启失效）
# 带 LRU 上限（_COLLECTS_CACHE_MAX），防止长时间运行后无限膨胀
_COLLECTS_CACHE: dict = {}
_CN_CACHE: dict = {}   # id -> infobox「简体中文名」；与收藏数同锁、同步淘汰
_COLLECTS_LOCK = threading.Lock()
_COLLECTS_CACHE_MAX = 5000
# 超限时淘汰最旧的一半，简单高效，避免频繁逐条 pop
_COLLECTS_EVICT_BATCH = _COLLECTS_CACHE_MAX // 2


def _cache_get(cid):
    """带锁读缓存；未命中返回 None。"""
    with _COLLECTS_LOCK:
        return _COLLECTS_CACHE.get(cid)


def _cn_get(cid):
    """带锁读简体中文名缓存；未命中返回 None。"""
    with _COLLECTS_LOCK:
        return _CN_CACHE.get(cid)


def _cache_set(cid, val, cn_val=""):
    """带锁写缓存；超限时按插入序淘汰最旧的一半。

    收藏数与简体中文名一起写入、一起淘汰，保证两者一致性。
    cn 为空也照常写入（值为 ""），避免空 cn 角色反复触发补拉详情。
    """
    with _COLLECTS_LOCK:
        _COLLECTS_CACHE[cid] = val
        _CN_CACHE[cid] = cn_val or ""
        if len(_COLLECTS_CACHE) > _COLLECTS_CACHE_MAX:
            for k in list(_COLLECTS_CACHE)[:_COLLECTS_EVICT_BATCH]:
                _COLLECTS_CACHE.pop(k, None)
                _CN_CACHE.pop(k, None)


def _get_collects(cid):
    """单角色收藏数 + 简体中文名查询（命中缓存直返；失败降级 0/""）。"""
    cached = _cache_get(cid)
    cn = _cn_get(cid)
    if cached is not None:
        return cached, cn or ""
    try:
        det = get_character_detail(cid)
        val = (det.get("stat") or {}).get("collects", 0)
        cn_val = get_cn_name(parse_infobox(det.get("infobox")))
    except Exception as e:
        logger.debug(f"_get_collects id={cid} 失败: {e}")
        val, cn_val = 0, ""
    _cache_set(cid, val, cn_val)
    return val, cn_val


def _attach_collects(results):
    """并发把搜索结果里每个角色的收藏数 + 简体中文名补齐，返回 {id: (collects, cn)}。

    简体中文名来自详情 infobox（搜索接口的 name_cn 经常为空），
    供排序精确匹配使用（如搜「我」→ 答案「我/わたし」的简体中文名是「我」）。
    并发上限 6，避免瞬间打爆 Bangumi；每个请求前让 150ms（=settings.RATE_LIMIT），
    与 bangumi.py 内置限速保持一致，避免触发 429。
    已命中的角色走缓存 0 开销。
    """
    ids = []
    for c in results:
        cid = c.get("id")
        if not cid:
            continue
        if _cache_get(cid) is None or _cn_get(cid) is None:
            ids.append(cid)
    if not ids:
        return {c.get("id"): (_cache_get(c.get("id")) or 0, _cn_get(c.get("id")) or "") for c in results if c.get("id")}

    out = {}

    def _one(cid):
        if _cache_get(cid) is not None and _cn_get(cid) is not None:
            return cid
        time.sleep(settings.RATE_LIMIT)
        v, cn = _get_collects(cid)
        with _COLLECTS_LOCK:
            out[cid] = (v, cn)
        return cid

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_one, ids))

    for cid, (v, cn) in out.items():
        _cache_set(cid, v, cn)
    return {c.get("id"): (_cache_get(c.get("id")) or 0, _cn_get(c.get("id")) or "") for c in results if c.get("id")}


def _download(url, dest_path):
    """下载图片到 dest_path，成功返回 True。

    修复说明：
    - 不再触碰 socket.setdefaulttimeout（进程级全局状态，多线程并发设置/恢复
      会互相覆盖，污染 AstrBot 其他网络组件）；超时完全交给 urlopen 的 timeout 参数。
    - 先写临时文件再 os.replace 原子替换，避免下载中断留下半截图片被当成有效缓存；
    - 下载完成后校验文件大小 > 0，空文件视为失败；
    - 失败按 settings.RETRY_TIMES 指数退避重试（限流 429/5xx/超时/连接错误），
      临时网络故障自动恢复；非限流 4xx（如图片 404）不重试，立即失败。
    """
    import os
    import urllib.request
    import urllib.error
    dest_path = Path(dest_path)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    req = urllib.request.Request(url, headers=UA)
    last = None
    for attempt in range(settings.RETRY_TIMES + 1):
        try:
            with urllib.request.urlopen(req, timeout=settings.IMAGE_HTTP_TIMEOUT) as r, open(tmp_path, "wb") as f:
                f.write(r.read())
            if tmp_path.stat().st_size <= 0:
                raise OSError("downloaded empty image")
            os.replace(tmp_path, dest_path)
            return True
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, ConnectionError, OSError) as e:
            last = e
            # 非限流/非5xx 的 HTTP 错误（如 404）不重试
            if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                break
            if attempt < settings.RETRY_TIMES:
                time.sleep(settings.RETRY_BASE * (2 ** attempt))
    raise last


def _image_path(wife):
    """返回净化后的立绘缓存路径：id 只保留数字，防止非法字符拼出危险路径。"""
    import re
    cid = re.sub(r"\D", "", str(wife.get("id") or "")) or "0"
    return IMAGE_DIR / f"{cid}.jpg"


def _ensure_wife_image(wife):
    """本地缓存立绘，失败返回 None（降级为无图）。

    - 文件名只保留 id 中的数字（净化，防止非法字符拼出危险路径）；
    - 已存在的缓存文件若大小为 0（历史遗留的坏文件），会重新下载覆盖。
    """
    img_path = _image_path(wife)
    if img_path.exists() and img_path.stat().st_size > 0:
        return img_path
    url = wife.get("image_url")
    if not url:
        return None
    try:
        _download(url, img_path)
        return img_path
    except Exception:
        return None


@register("astrbot_plugin_today_wife", "SR3MP", "专属老婆（Bangumi版）", "3.2.0")
class WifePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 从 WebUI 插件配置覆盖所有默认值
        settings.load(config)
        self.config = config
        DATA_DIR.mkdir(exist_ok=True)
        IMAGE_DIR.mkdir(exist_ok=True)

    # ---------- 帮助文本（动态生成） ----------

    def _help_text(self):
        """根据当前 settings 动态生成帮助文本。"""
        solved_rewards = []
        for i in range(1, settings.DAILY_SOLVED_LIMIT + 1):
            r = settings.GUESS_BASE + settings.SOLVED_BONUS.get(i, 0)
            solved_rewards.append(f"+{r}")
        reward_str = "/".join(solved_rewards)
        n_solved = settings.DAILY_SOLVED_LIMIT

        lines = [
            "👑 专属老婆 · 游戏帮助",
            "━━━━━━━━━━━━━━━━",
            "",
            "🎮 玩法流程",
            "  /抽老婆 抽神秘老婆",
            "  → /老婆猜 猜名字",
            "  → 猜对迎娶进后宫",
            "  🎴 想看看能抽到什么？/老婆卡池",
            "",
            "📖 规则说明",
            f"  · 每天可无限猜，猜错不扣分",
            f"  · 每天前 {n_solved} 次猜对给积分",
            f"    第1~{n_solved}次：{reward_str}",
            f"  · 每日签到 +{settings.CHECKIN_REWARD} 积分",
            f"  · 迎娶上限 {settings.DAILY_WED_INIT} 次/天，可升级",
            f"  · 后宫容量初始 {settings.CAPACITY_INIT} 格，可扩容",
            f"  · 扩容 {settings.CAPACITY_EXPAND_BASE}×n ｜ 升迎娶 {settings.WED_UPGRADE_BASE}×n ｜ 请回 {settings.REINVITE_COST}",
            "",
            "📋 命令列表",
            "  /抽老婆            抽专属老婆",
            "  /老婆猜 <名字>     提交猜测",
            "  /老婆迎娶          迎娶刚猜对的老婆",
            "  /老婆放弃          放弃正在猜的老婆，重新抽取",
            f"  /老婆签到          每日 +{settings.CHECKIN_REWARD} 积分",
            "  /老婆后宫          查看后宫、积分与冷宫",
            "  /老婆扩容          扩充后宫容量",
            "  /老婆升迎娶        提升每日迎娶次数",
            "  /老婆移除 <名字>   移除后宫成员（打入冷宫）",
            "  /老婆请回 <名字>   花积分请回被移除成员",
            "  /老婆群榜          查看全群老婆现状",
            "  /老婆卡池          卡池总览/查角色/查作品",
            "  /老婆立绘 <名字>   查看角色立绘（无图自动下载）",
            "  /老婆帮助          查看帮助与规则说明",
            "━━━━━━━━━━━━━━━━",
            "🔗 数据源：Bangumi API (bgm.tv)",
        ]
        return "\n".join(lines)

    # ---------- 个人老婆（抽→猜→迎娶） ----------

    def _get_personal_wife(self, uid):
        """取用户个人待猜的神秘老婆（含聚合线索）。没有则抽一个，返回 None 表示抽取失败。

        v3.2：抽取优先走【本地库】（每日同步一次，零 API，秒回）；
        本地库缺失/过期时才回退实时 API 抽取。
        """
        uid = str(uid)
        active = harem.get_active_wife(uid)
        if active and active.get("_clue_version") == CLUE_VERSION:
            return active
        if active:
            # 旧版缓存：保留当前角色与猜题进度，就地重聚评分
            old_cid = active.get("id")
            old_hint_idx = active.get("hint_idx", 1)
            logger.info(f"个人老婆评分缓存版本过期(v={active.get('_clue_version')})，就地重聚 id={old_cid} uid={uid}")
            # 注意：不要在重聚前清空 active_wife——聚合失败会丢掉用户进行中的老婆。
            # 先重聚，成功后再覆盖；失败则保留旧数据，下次 /抽老婆 会再次重试重聚。
            try:
                new_info = collect_character_info(old_cid)
            except Exception as e:
                logger.warning(f"重聚角色失败 id={old_cid}: {e}")
                return None
            new_info["hints"] = build_hints(new_info, settings.HINTS_TOTAL)
            new_info["hint_idx"] = old_hint_idx
            # 优先用新版聚合出的别名，缺失时才回退旧数据
            new_info["aliases"] = new_info.get("aliases") or active.get("aliases") or []
            new_info["image_url"] = active.get("image_url")
            new_info["_picked_at"] = active.get("_picked_at") or time.strftime("%Y-%m-%d %H:%M:%S")
            harem.set_active_wife(uid, new_info)
            return new_info

        # 抽一个全新的个人老婆：优先本地库
        t0 = time.time()
        local_db.ensure_local_db_bg()      # 过期则后台悄悄重建，不阻塞玩家
        wife = local_db.random_wife_local() or random_wife(min_collects=settings.MIN_COLLECTS)
        if not wife:
            return None

        # 聚合线索：优先用本地线索缓存，没有再实时聚合（猜题对比需要作品/年代/评分等维度）
        info = local_db.get_clue(wife["id"])
        if info and info.get("_clue_version") != CLUE_VERSION:
            info = None            # 旧版线索缓存作废，重新聚合
        if not info:
            try:
                info = collect_character_info(wife["id"])
            except Exception as e:
                logger.warning(f"抽老婆：聚合角色失败 id={wife['id']}: {e}")
                return None
        else:
            info = dict(info)  # 缓存里取出的要复制，避免互改
        info["hints"] = build_hints(info, settings.HINTS_TOTAL)
        info["aliases"] = wife.get("aliases") or []
        info["image_url"] = wife.get("image_url")
        info["hint_idx"] = 1   # 已展示线索条数（猜错自动+1）
        info["_picked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        info["_cost"] = round(time.time() - t0, 1)
        harem.set_active_wife(uid, info)
        return info

    @filter.command("抽老婆")
    async def on_draw_wife(self, event: AstrMessageEvent):
        """抽一位只属于自己的神秘老婆，猜对即可迎娶进后宫。"""
        uid = self._uid_with_name(event)
        # 已有进行中的老婆时，/抽老婆 不重新抽，而是展示当前线索进度（行为与文案统一）
        existing = harem.get_active_wife(uid)
        if existing and existing.get("_clue_version") == CLUE_VERSION:
            wife = existing
            yield event.plain_result("💖 你已经有专属老婆在猜啦，这是当前线索进度：")
        else:
            yield event.plain_result("🎴 正在为你抽取一位专属老婆...")
            wife = await asyncio.to_thread(self._get_personal_wife, uid)   # 同步网络调用挪到线程池，避免阻塞事件循环
            if not wife:
                yield event.plain_result("❌ 抽取失败：Bangumi 网络波动，请稍后再试。")
                return
            yield event.plain_result("🔍 你的专属神秘老婆已就位！")
        hints = wife.get("hints") or []
        # 展示当前进度对应的线索（猜错解锁后重看 /抽老婆 应看到最新一条）
        idx = int(wife.get("hint_idx", 1) or 1)
        cur = hints[idx - 1] if hints and idx <= len(hints) else (hints[-1] if hints else "无")
        yield event.plain_result(
            "━━━━━━━━━━━━━━\n"
            f"👀 线索{idx}：{cur}\n"
            "━━━━━━━━━━━━━━\n"
            "🎮 用「/老婆猜 <名字>」提交答案\n"
            "💡 猜错会自动解锁下一条线索\n"
            "🏆 猜对可得积分，并用「/老婆迎娶」迎娶进后宫！"
        )

    @filter.command("老婆迎娶")
    async def on_accept_wife(self, event: AstrMessageEvent):
        """把刚猜对的老婆迎娶进后宫。"""
        uid = self._uid_with_name(event)
        pending = harem.get_pending_accept(uid)
        if not pending:
            yield event.plain_result("没有待迎娶的老婆～先 /抽老婆 并猜对，再用 /老婆迎娶")
            return
        ok, msg = harem.add_to_harem(uid, {"id": pending["id"], "name": pending["name"]})
        # 无论成败都清 pending：失败时（已在后宫/容量满等）也释放资格并明确提示，
        # 避免用户反复 /老婆迎娶 看到同一句错误，且无法重新抽/猜
        harem.set_pending_accept(uid, None)
        if ok:
            yield event.plain_result(f"🎉 {msg}\n发 /老婆后宫 查看你的后宫")
        else:
            yield event.plain_result(f"❌ {msg}\n本次迎娶未成功，资格已释放；可重新 /抽老婆 再猜一位。")

    @filter.command("老婆猜")
    async def on_guess(self, event: AstrMessageEvent, name: GreedyStr):
        """提交猜测：/老婆猜 <角色名>，猜自己的专属老婆。"""
        name = (name or "").strip()
        if not name:
            yield event.plain_result("格式：/老婆猜 <角色名>，例如 /老婆猜 鹿目圆")
            return

        uid = self._uid_with_name(event)
        personal = harem.get_active_wife(uid)
        if not personal:
            yield event.plain_result("你还没有专属老婆～先发 /抽老婆 抽一位再来猜吧！")
            return

        async for msg in self._guess_personal(event, uid, personal, name):
            yield msg

    async def _guess_personal(self, event, uid, answer, name):
        """个人老婆猜题：搜索→对比→结算积分/迎娶资格。"""
        try:
            resp = await asyncio.to_thread(search_characters, name, settings.GUESS_SEARCH_LIMIT)   # 条数可配置（原硬编码 16）
            results = resp.get("data") or []
        except Exception as e:
            logger.warning(f"猜老婆：搜索角色失败: {e}")
            yield event.plain_result("❌ 搜索 Bangumi 失败，请稍后再试。")
            return

        if not results:
            yield event.plain_result("🤔 Bangumi 上没搜到这个角色，换个名字试试？")
            return

        # 排序：归一化精确匹配为主，收藏数次之；低收藏占位角色不参与精确优先。
        #
        # 为什么精确匹配（归一化后）优先？
        #   Bangumi 搜索词与结果存在简繁差异：搜「天野阳菜」返回「天野陽菜」(繁体)，
        #   原逻辑 name==kw 直接判不等 → 精确 tiebreaker 失效 → 纯拼收藏数，
        #   导致收藏 513 的天野遠子顶掉收藏 434 的天野陽菜(用户明明搜的就是她)。
        #   用 zhconv 归一化简繁后比较，可恢复精确匹配。
        #
        # 为什么还要过滤低收藏占位角色？
        #   搜「三笠」时存在 name 恰为「三笠」的路人角色(收藏 0/1)，若精确匹配无条件优先，
        #   会把收藏 1014 的三笠·阿克曼顶掉。收藏数 >= 10 视为"真实角色"才享受精确优先。
        #
        # 为什么还要匹配 infobox 简体中文名（_CN_CACHE）？
        #   部分角色搜索接口的 name_cn 为空，简体中文名藏在详情 infobox 里：
        #   搜「我」时答案「我(わたし)」的 name="わたし"、name_cn 为空，但 infobox
        #   简体中文名 =「我」。若不补齐，精确匹配失效 → 纯拼收藏数 →
        #   收藏 829 的我妻由乃顶掉收藏 126 的正确答案。_attach_collects 反正要拉
        #   详情取收藏数，顺带提取简体中文名，零额外请求。
        #
        # 为什么收藏数仍保留？
        #   非精确命中的场景(前缀/日文名搜索)下，收藏数 = 全站关注度，比 Bangumi 原始序
        #   (基于字符重合度，搜「三笠」时 14 个"三笠XX"前缀路人挤占前 14 位)更可靠。
        kw = (name or "").strip()
        kw_norm = zhconv.convert(kw, "zh-hans")
        _COLLECTS_MAP = _attach_collects(results)

        def _rank(c):
            nc = zhconv.convert((c.get("name_cn") or "").strip(), "zh-hans")
            nm = zhconv.convert((c.get("name") or "").strip(), "zh-hans")
            collects, cn = _COLLECTS_MAP.get(c.get("id"), (0, ""))
            cn = zhconv.convert((cn or "").strip(), "zh-hans")
            exact = 0 if (nc == kw_norm or nm == kw_norm or cn == kw_norm) and collects >= 10 else 1
            return (exact, -collects)
        results.sort(key=_rank)

        cid = results[0]["id"]
        try:
            guess_info = await asyncio.to_thread(collect_character_info, cid)   # 同步网络调用挪到线程池
        except Exception as e:
            logger.warning(f"猜老婆：聚合玩家猜测失败 id={cid}: {e}")
            yield event.plain_result("❌ 解析该角色失败，请换个名字。")
            return

        lines, hit = compare(guess_info, answer)
        guess_name = guess_info.get("name_cn") or guess_info.get("name") or name
        result = [f"🎯 你猜的是：「{guess_name}」", "━━━━━━━━━━━━━━"] + lines

        if hit:
            # 原子结算：发积分 + 清待猜老婆 + 写入待迎娶，一个锁内完成（见 harem.settle_correct_guess）
            settle = harem.settle_correct_guess(uid, answer)
            local_db.cache_clue(answer["id"], answer)  # 线索写缓存，下次抽到秒回
            reward_line = f"积分 +{settle.get('reward', 0)}（今日第 {settle.get('n', 0)} 次猜对）" if settle.get("reward") else "今日积分次数已满，本次无积分"
            result += [
                "━━━━━━━━━━━━━━",
                "🎉🎉 答对了！！你的老婆是：",
                f"💕 {answer.get('name_cn') or answer.get('name')}（{answer.get('name')}）",
                f"💰 {reward_line}",
                "━━━━━━━━━━━━━━",
                "📬 发「/老婆迎娶」把她迎娶进后宫！",
            ]
            yield event.plain_result("\n".join(result))

            img_path = _image_path(answer)
            if not img_path.exists():
                img_path = await asyncio.to_thread(_ensure_wife_image, {"id": answer["id"], "image_url": answer.get("image_url")})
            if img_path:
                yield event.image_result(str(img_path))
        else:
            hints = answer.get("hints") or []
            idx = int(answer.get("hint_idx", 1))       # 已展示线索条数
            if idx < len(hints):
                result.append("━━━━━━━━━━━━━━")
                result.append(f"❌ 不是她，再猜！已自动解锁线索第 {idx+1} 条：{hints[idx]}")
                answer["hint_idx"] = idx + 1
                harem.set_active_wife(uid, answer)
            else:
                result.append("━━━━━━━━━━━━━━")
                result.append("❌ 不是她，再猜！线索已全部用完，只能靠感觉了~")
            yield event.plain_result("\n".join(result))

    @filter.command("老婆放弃")
    async def on_give_up_wife(self, event: AstrMessageEvent):
        """放弃正在猜的老婆：清空当前待猜老婆，可重新 /抽老婆。"""
        uid = self._uid_with_name(event)
        personal = harem.get_active_wife(uid)
        if not personal:
            yield event.plain_result("你现在没有正在猜的老婆～先 /抽老婆 抽一位再来吧！")
            return
        name = personal.get("name_cn") or personal.get("name") or "这位神秘老婆"
        harem.clear_active_wife(uid)
        yield event.plain_result(
            f"💔 已放弃「{name}」，她回到了人海之中…"
        )
        yield event.plain_result("发 /抽老婆 重新抽取一位专属老婆吧！")

    # ---------- 后宫与积分 ----------

    @filter.command("老婆签到")
    async def on_checkin(self, event: AstrMessageEvent):
        """每日签到领积分。"""
        uid = self._uid_with_name(event)
        ok, points, msg = harem.checkin(uid)
        if ok:
            yield event.plain_result(f"✅ {msg}\n发 /老婆后宫 查看你的后宫和积分")
        else:
            yield event.plain_result(msg)

    @filter.command("老婆后宫")
    async def on_harem(self, event: AstrMessageEvent):
        """查看自己的后宫与积分。"""
        uid = self._uid_with_name(event)
        v = harem.profile_view(uid)
        lines = [
            "👑 你的后宫",
            "━━━━━━━━━━━━━━",
            f"💳 积分：{v['points']}",
            f"🏠 容量：{v['capacity']} 格（已用 {len(v['harem'])}）",
            f"💍 今日迎娶剩余：{v['wed_left']} 次",
            f"🎯 今日猜对积分剩余次数：{v['solved_left']} 次",
        ]
        if v["harem"]:
            lines.append("━━━━━━━━━━━━━━")
            lines.append("💕 后宫成员：")
            for i, h in enumerate(v["harem"], 1):
                lines.append(f"  {i}. 💕 {h['name']}")
        else:
            lines.append("━━━━━━━━━━━━━━")
            lines.append("  💕 后宫成员：后宫里还没有老婆…用 /抽老婆 开始吧！")

        # ❄️ 打入冷宫板块
        lines.append("━━━━━━━━━━━━━━")
        lines.append("❄️ 打入冷宫：")
        if v["removed"]:
            for i, r in enumerate(v["removed"], 1):
                lines.append(f"  {i}. ❄️ {r['name']}")
        else:
            lines.append("  冷宫空空如也～")

        lines += [
            "━━━━━━━━━━━━━━",
            "📈 /老婆扩容 扩后宫 · 💍 /老婆升迎娶 加每日次数",
            "🗑️ /老婆移除 <名字> 打入冷宫 · ↩️ /老婆请回 <名字> 花积分请回",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("老婆扩容")
    async def on_expand(self, event: AstrMessageEvent):
        """花积分扩充后宫容量（递增收费）。"""
        uid = self._uid_with_name(event)
        ok, msg = harem.expand_capacity(uid)
        yield event.plain_result(("✅ " + msg) if ok else ("❌ " + msg))

    @filter.command("老婆升迎娶")
    async def on_upgrade_wed(self, event: AstrMessageEvent):
        """花积分提升每日迎娶次数（递增收费）。"""
        uid = self._uid_with_name(event)
        ok, msg = harem.upgrade_wed_limit(uid)
        yield event.plain_result(("✅ " + msg) if ok else ("❌ " + msg))

    @filter.command("老婆立绘")
    async def on_portrait(self, event: AstrMessageEvent, name: GreedyStr):
        """查看老婆立绘：本地图优先，无则按 image_url 尝试下载。"""
        name = (name or "").strip()
        if not name:
            yield event.plain_result("格式：/老婆立绘 <名字>，例如 /老婆立绘 伊吹风子")
            return
        db = local_db.get_local_db()
        entries = (db or {}).get("entries") or []
        target = zhconv.convert(name, "zh-hans")

        def _names(e):
            return [e.get("name_cn") or "", e.get("name") or ""] + list(e.get("aliases") or [])

        hit = None
        # 第一轮：精确匹配（含繁简归一）
        for e in entries:
            if any(zhconv.convert(n, "zh-hans") == target for n in _names(e) if n):
                hit = e
                break
        # 第二轮：包含匹配
        if not hit:
            for e in entries:
                if any(target in zhconv.convert(n, "zh-hans") for n in _names(e) if n):
                    hit = e
                    break
        if not hit:
            yield event.plain_result(f"❌ 本地库（{len(entries)} 位角色）里没找到「{name}」，试试全名或别名。")
            return
        img_path = _image_path(hit)
        if not (img_path.exists() and img_path.stat().st_size > 0):
            img_path = await asyncio.to_thread(
                _ensure_wife_image,
                {"id": hit.get("id"), "image_url": hit.get("image_url")},
            )
        if img_path and img_path.exists() and img_path.stat().st_size > 0:
            shown = hit.get("name_cn") or hit.get("name")
            yield event.plain_result(f"🖼 {shown}（Bangumi id {hit.get('id')}）")
            yield event.image_result(str(img_path))
        else:
            yield event.plain_result(
                f"❌ 「{hit.get('name_cn') or hit.get('name')}」本地无图且下载失败（图床被网络阻断），等待批量补图。"
            )

    @filter.command("老婆卡池")
    async def on_wife_pool(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """查询老婆卡池：/老婆卡池 总览 ｜ /老婆卡池 <角色名> 查角色 ｜ /老婆卡池 作品 <作品名> 查作品。"""
        db = local_db.get_local_db()
        entries = (db or {}).get("entries") or []
        if not entries:
            yield event.plain_result("❌ 本地卡池为空，请稍后重建或检查 wife_db.json。")
            return

        def norm(s):
            return zhconv.convert((s or "").strip(), "zh-hans")

        def has_img(e):
            p = _image_path(e)
            return p.exists() and p.stat().st_size > 0

        q = norm(query)

        # ── 无参数：卡池总览 ──
        if not q:
            works = {}
            noimg = 0
            for e in entries:
                w = e.get("subject_name") or "未知作品"
                works[w] = works.get(w, 0) + 1
                if not has_img(e):
                    noimg += 1
            top = sorted(entries, key=lambda e: e.get("collects") or 0, reverse=True)[:10]
            lines = [
                "🎴 老婆卡池 · 总览",
                "━━━━━━━━━━━━━━━━",
                f"💃 收录角色：{len(entries)} 位",
                f"🎬 覆盖作品：{len(works)} 部",
                f"🖼 立绘齐全：{len(entries) - noimg}/{len(entries)}（缺 {noimg} 图）",
                "",
                "🔥 最热 TOP10",
            ]
            for i, e in enumerate(top, 1):
                nm = e.get("name_cn") or e.get("name") or f"id{e.get('id')}"
                lines.append(f" {i:>2}. {nm}（{e.get('collects')} 收藏）")
            lines += [
                "━━━━━━━━━━━━━━━━",
                "🔍 /老婆卡池 <名字>     查角色",
                "🎬 /老婆卡池 作品 <作品> 查作品",
            ]
            yield event.plain_result("\n".join(lines))
            return

        # ── 作品查询：/老婆卡池 作品 <作品名> ──
        if q.startswith("作品"):
            kw = norm(q[2:])
            hits = [e for e in entries if kw and kw in norm(e.get("subject_name") or "")]
            if not hits:
                yield event.plain_result(f"❌ 卡池作品里没找到「{query[2:].strip()}」，可用 /老婆卡池 看总览，或 /老婆卡池 <角色名> 查角色。")
                return
            lines = [f"🎬 《{hits[0].get('subject_name')}》在卡池 {len(hits)} 位", "━━━━━━━━━━━━━━━━"]
            for e in sorted(hits, key=lambda e: e.get("collects") or 0, reverse=True):
                nm = e.get("name_cn") or e.get("name") or f"id{e.get('id')}"
                mark = "✅" if has_img(e) else "⬜"
                lines.append(f" {mark} {nm}（{e.get('collects')} 收藏）")
            lines.append("━━━━━━━━━━━━━━━━")
            lines.append("⬜ = 缺立绘，等待补图")
            yield event.plain_result("\n".join(lines))
            return

        # ── 角色查询：/老婆卡池 <角色名/别名> ──
        def names(e):
            return [e.get("name_cn") or "", e.get("name") or ""] + list(e.get("aliases") or [])

        hit = None
        for e in entries:
            if any(norm(n) == q for n in names(e) if n):
                hit = e
                break
        if not hit:
            for e in entries:
                if any(q in norm(n) for n in names(e) if n):
                    hit = e
                    break
        if not hit:
            yield event.plain_result(
                f"❌ 卡池（{len(entries)} 位角色）里没有「{query.strip()}」，试试全名、别名，或 /老婆卡池 看总览。"
            )
            return

        nm = hit.get("name_cn") or hit.get("name") or f"id{hit.get('id')}"
        lines = [
            f"💜 {nm}",
            "━━━━━━━━━━━━━━━━",
            f"🎬 作品：《{hit.get('subject_name') or '未知'}》",
            f"⭐ Bangumi 收藏：{hit.get('collects')}",
        ]
        if hit.get("cv"):
            lines.append(f"🎤 CV：{hit.get('cv')}")
        if hit.get("age"):
            lines.append(f"🎂 年龄：{hit.get('age')}")
        if hit.get("birth"):
            lines.append(f"📅 生日：{hit.get('birth')}")
        lines.append(f"🖼 立绘：{'✅ 已就绪' if has_img(hit) else '⬜ 缺图中（等待补图）'}")
        alias = (hit.get("aliases") or [])[:6]
        if alias:
            lines.append(f"🏷 别名：{'、'.join(alias)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("老婆移除")
    async def on_remove(self, event: AstrMessageEvent, name: GreedyStr):
        """把后宫里的老婆移除。"""
        uid = self._uid_with_name(event)
        name = (name or "").strip()
        if not name:
            yield event.plain_result("格式：/老婆移除 <老婆名字>，例如 /老婆移除 鹿目圆")
            return
        ok, msg = harem.remove_from_harem(uid, name)
        yield event.plain_result(("✅ " + msg) if ok else ("❌ " + msg))

    @filter.command("老婆请回")
    async def on_reinvite(self, event: AstrMessageEvent, name: GreedyStr):
        """花积分把已移除的老婆请回后宫。"""
        uid = self._uid_with_name(event)
        name = (name or "").strip()
        if not name:
            yield event.plain_result("格式：/老婆请回 <老婆名字>，例如 /老婆请回 鹿目圆")
            return
        ok, msg = harem.reinvite(uid, name)
        yield event.plain_result(("✅ " + msg) if ok else ("❌ " + msg))

    # ---------- 全群老婆现状榜 ----------

    # 平台占位昵称黑名单：这些值视为"无有效昵称"
    _UNKNOWN_SENDER_NAMES = {
        "", "none", "null", "unknown", "未知", "无名", "匿名",
        "n/a", "na", "bot", "qq", "wx", "villa", "sender",
    }

    @classmethod
    def _normalize_sender_name(cls, value):
        """过滤平台占位昵称，保留可读名称；无效则返回 None。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in cls._UNKNOWN_SENDER_NAMES:
            return None
        return text

    @staticmethod
    def _raw_get(obj, key: str):
        """安全地取属性/字典键，不存在返回 None。"""
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    @classmethod
    def _call_event_getter(cls, event, method_name: str):
        """安全调用事件方法（如 get_sender_name），异常返回 None。"""
        getter = getattr(event, method_name, None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as exc:
            logger.debug(f"读取事件方法 {method_name} 失败: {exc}")
            return None

    @staticmethod
    def _format_raw_user_name(raw_user):
        """从原始用户对象提取可读名字（username/nickname/full_name/first+last）。"""
        if raw_user is None:
            return None

        def g(key):
            if isinstance(raw_user, dict):
                v = raw_user.get(key)
            else:
                v = getattr(raw_user, key, None)
            if v is None:
                return None
            text = str(v).strip()
            return text or None

        for key in ("username", "nickname", "full_name"):
            cand = g(key)
            if cand:
                return cand
        first = g("first_name")
        last = g("last_name")
        full = " ".join(part for part in (first, last) if part).strip()
        if full:
            return full
        return None

    @classmethod
    def _resolve_sender_name(cls, event):
        """按 livingmemory 同款 fallback 链解析发送者昵称（跨平台通用）。
        顺序：get_sender_name → sender_name → first/last_name → nickname/card
              → raw_message 深挖 from_user/effective_user → author.username/member.nick
        全部失败返回 None（由调用方 fallback 到 uid）。
        """
        # ① 标准接口
        name = cls._normalize_sender_name(
            cls._call_event_getter(event, "get_sender_name")
        )
        if name:
            return name
        name = cls._normalize_sender_name(cls._raw_get(event, "sender_name"))
        if name:
            return name

        message_obj = getattr(event, "message_obj", None)
        raw_sender = getattr(message_obj, "sender", None)

        # ② Telegram 风格 first_name + last_name
        first = cls._normalize_sender_name(cls._raw_get(raw_sender, "first_name"))
        last = cls._normalize_sender_name(cls._raw_get(raw_sender, "last_name"))
        full = " ".join(part for part in (first, last) if part).strip()
        if full:
            return full

        # ③ OneBot 风格 nickname / card（群名片）
        nickname = cls._normalize_sender_name(cls._raw_get(raw_sender, "nickname"))
        if nickname:
            return nickname
        card = cls._normalize_sender_name(cls._raw_get(raw_sender, "card"))
        if card:
            return card

        # ④ 从原始消息深挖 from_user / effective_user
        raw_message = getattr(message_obj, "raw_message", None)
        for source in (
            raw_message,
            cls._raw_get(raw_message, "message"),
            cls._raw_get(raw_message, "effective_message"),
            cls._raw_get(raw_message, "callback_query"),
        ):
            raw_user = cls._raw_get(source, "from_user")
            if raw_user is not None:
                candidate = cls._format_raw_user_name(raw_user)
                if candidate:
                    return candidate
        effective_user = cls._raw_get(raw_message, "effective_user")
        if effective_user is not None:
            candidate = cls._format_raw_user_name(effective_user)
            if candidate:
                return candidate

        # ⑤ botpy 风格 author.username / author.member.nick（QQ 频道场景）
        author = cls._raw_get(raw_message, "author")
        if author is not None:
            username = cls._normalize_sender_name(cls._raw_get(author, "username"))
            if username:
                return username
            member = cls._raw_get(author, "member")
            nick = cls._normalize_sender_name(cls._raw_get(member, "nick"))
            if nick:
                return nick

        return None

    def _uid_with_name(self, event: AstrMessageEvent):
        """取 sender_id 并顺手把昵称记入档案（群榜展示用）。"""
        uid = str(event.get_sender_id() or "")
        if not uid:
            return uid
        try:
            name = self._resolve_sender_name(event)
            if name and name != uid:
                harem.remember_name(uid, name)
        except Exception as e:
            logger.debug(f"记录昵称失败: {e}")
        return uid

    @filter.command("老婆群榜")
    async def on_group_leaderboard(self, event: AstrMessageEvent):
        """查看全群老婆现状排行榜（按后宫大小排序）。"""
        # 仅群聊生效
        if event.is_private_chat():
            yield event.plain_result("本指令仅在群聊中可用～私聊下用 /老婆后宫 查看自己的后宫吧！")
            return

        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("❌ 无法获取群 ID，指令失败。")
            return

        # 拿当前群成员（uid -> 群名片/nickname 的映射）
        member_map = {}  # uid -> display_name
        try:
            if hasattr(event, "bot"):
                member_list = await event.bot.call_action(
                    "get_group_member_list",
                    group_id=int(group_id) if group_id.isdigit() else group_id,
                )
                if isinstance(member_list, list):
                    for m in member_list:
                        uid = str(m.get("user_id", ""))
                        # 群名片（card）优先，其次 nickname
                        name = m.get("card") or m.get("nickname") or uid
                        member_map[uid] = name
        except Exception as e:
            logger.warning(f"获取群成员列表失败: {e}")
            member_map = {}

        # 读取所有用户档案
        all_profiles = harem.get_all_profiles()
        if not all_profiles:
            yield event.plain_result("🏆 全群后宫榜：目前还没有人拥有老婆，快来 /抽老婆 当第一个！")
            return

        # 过滤出当前群成员（若有成员列表则只算群内人；没有则展示所有人）
        if member_map:
            profiles = {
                uid: p for uid, p in all_profiles.items()
                if uid in member_map and p.get("harem")
            }
        else:
            profiles = {
                uid: p for uid, p in all_profiles.items() if p.get("harem")
            }

        if not profiles:
            yield event.plain_result("🏆 全群后宫榜：本群还没有人拥有老婆，快来 /抽老婆 当第一个！")
            return

        # 按后宫大小排序
        sorted_profiles = sorted(
            profiles.items(),
            key=lambda kv: len(kv[1].get("harem") or []),
            reverse=True,
        )

        lines = [
            "🏆 全群老婆现状榜",
            f"（共 {len(sorted_profiles)} 位有后宫 · 按后宫大小排序）",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        for rank, (uid, p) in enumerate(sorted_profiles, 1):
            name = member_map.get(uid) or p.get("display_name") or uid
            # 去掉过长/乱码的 uid
            if len(name) > 20:
                name = name[:18] + "…"
            harem_count = len(p.get("harem") or [])
            capacity = p.get("capacity", 3)
            removed = p.get("removed") or []
            points = p.get("points", 0)
            # 前三名换花哨头饰
            if rank == 1:
                crown = "👑"
            elif rank == 2:
                crown = "🥈"
            elif rank == 3:
                crown = "🥉"
            else:
                crown = f"  {rank:02d}"
            lines.append(
                f"{crown} {name}  💕{harem_count}/{capacity}格 "
                f"💳{points} "
                f"{'❄️' + str(len(removed)) if removed else ''}"
            )
            # 后宫成员名单
            members = [h.get("name", "?") for h in (p.get("harem") or [])]
            if members:
                # 名字过长时分两行展示
                text = "、".join(members)
                if len(text) <= 30:
                    lines.append(f"      💕 {text}")
                else:
                    first = members[:3]
                    lines.append(f"      💕 {'、'.join(first)} 等{len(members)}位")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💕=后宫老婆  💳=积分  ❄️=冷宫人数")
        lines.append("用 /老婆后宫 查看自己的详细后宫")

        yield event.plain_result("\n".join(lines))

    # ---------- 帮助 ----------

    @filter.command("老婆帮助")
    async def on_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

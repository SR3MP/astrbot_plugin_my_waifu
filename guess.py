"""
猜题游戏引擎
============
抽一个「神秘老婆」，玩家猜名字。
每次猜测后给出与答案的对比反馈（绿=命中 / 黄=部分 / 红=未中 + 方向提示）。

对比维度：
    1. 出演作品重合度   （answer.work_names vs guess.work_names）
    2. 年代范围         （更早 / 更晚）
    3. 最高评分         （更高 / 更低）
    4. CV               （命中 / 未中）
    5. 性别             （命中 / 未中）
    6. 来源类型         （作品类型：动画/游戏）

难度分级（渐进提示）：
    - 每猜一次会附带一条渐进线索（猜错自动解锁下一条）
    - 线索顺序：0作品数量 1年代范围 2最高评分 3作品类型 4CV 5作品名 6立绘
"""
from difflib import SequenceMatcher

GREEN = "🟢"
YELLOW = "🟡"
RED = "🔴"


def name_variants(info):
    """把角色名展开成所有可匹配的写法：原名/中文名/别名。"""
    names = set()
    if info.get("name"):
        names.add(info["name"].strip())
    if info.get("name_cn"):
        names.add(info["name_cn"].strip())
    for a in info.get("aliases") or []:
        if a and len(a) <= 40:
            names.add(a)
    return {n for n in names if n}


def similarity(a, b):
    """两个字符串相似度 0-1。"""
    if not a or not b:
        return 0
    return SequenceMatcher(None, a, b).ratio()


def best_match_names(guess_info, answer_info):
    """玩家猜的名字 vs 答案所有名字，取最大相似度。"""
    guess_names = name_variants(guess_info)
    answer_names = name_variants(answer_info)
    best = 0.0
    hit_name = None
    for g in guess_names:
        for a in answer_names:
            if g == a:
                return 1.0, a
            s = similarity(g, a)
            if s > best:
                best = s
                hit_name = a
    return best, hit_name


def compare(guess_info, answer_info):
    """对比玩家猜的角色与答案，返回反馈行列表 + 是否命中。"""
    lines = []

    # 1. 出演作品重合
    guess_works = set(guess_info.get("work_names") or [])
    answer_works = set(answer_info.get("work_names") or [])
    overlap = guess_works & answer_works
    if overlap:
        lines.append(f"{GREEN} 出演作品命中：{'、'.join(list(overlap)[:3])}")
    else:
        lines.append(f"{RED} 出演作品完全没重合")

    # 2. 年代范围
    g_latest = guess_info.get("latest_year")
    a_latest = answer_info.get("latest_year")
    g_earliest = guess_info.get("earliest_year")
    a_earliest = answer_info.get("earliest_year")
    if a_latest and g_latest:
        if a_latest > g_latest + 1:
            lines.append(f"{YELLOW} 年代：答案更晚（你猜最近 {g_latest} 年）")
        elif a_latest < g_latest - 1:
            lines.append(f"{YELLOW} 年代：答案更早（你猜最近 {g_latest} 年）")
        else:
            lines.append(f"{GREEN} 年代接近（答案最近 {a_latest} 年）")
    if a_earliest and g_earliest:
        if a_earliest < g_earliest - 1:
            lines.append(f"{YELLOW} 首次登场更早（答案最早 {a_earliest} 年）")
        elif a_earliest > g_earliest + 1:
            lines.append(f"{YELLOW} 首次登场更晚（答案最早 {a_earliest} 年）")

    # 3. 评分
    a_rating = answer_info.get("highest_rating") or 0
    g_rating = guess_info.get("highest_rating") or 0
    if a_rating and g_rating:
        if a_rating > g_rating + 0.3:
            lines.append(f"{YELLOW} 评分：答案更高（{a_rating:.1f} vs 你猜 {g_rating:.1f}）")
        elif a_rating < g_rating - 0.3:
            lines.append(f"{YELLOW} 评分：答案更低（{a_rating:.1f} vs 你猜 {g_rating:.1f}）")
        else:
            lines.append(f"{GREEN} 评分接近（答案最高 {a_rating:.1f}）")

    # 4. CV
    guess_cv = set(guess_info.get("cvs") or [])
    answer_cv = set(answer_info.get("cvs") or [])
    cv_hit = guess_cv & answer_cv
    if cv_hit:
        lines.append(f"{GREEN} CV 命中：{'、'.join(list(cv_hit)[:3])}")
    elif answer_cv and guess_cv:
        lines.append(f"{RED} CV 没重合（答案 CV：{'、'.join(list(answer_cv)[:3])}）")

    # 5. 来源类型
    answer_types = {w.get("type") for w in answer_info.get("works") or []} - {None}
    guess_types = {w.get("type") for w in guess_info.get("works") or []} - {None}
    if answer_types and guess_types:
        shared = answer_types & guess_types
        if shared:
            lines.append(f"{GREEN} 类型命中：{'、'.join(shared)}")
        else:
            lines.append(f"{RED} 类型不符（答案涉及：{'、'.join(sorted(answer_types))}）")

    # 6. 来源标签 tag（游戏改/漫画改/原创等）
    answer_tags = set(answer_info.get("tags") or [])
    guess_tags = set(guess_info.get("tags") or [])
    if answer_tags and guess_tags:
        tag_shared = answer_tags & guess_tags
        if tag_shared:
            lines.append(f"{GREEN} 来源标签命中：{'、'.join(sorted(tag_shared))}")
        else:
            lines.append(f"{RED} 来源标签不符（答案：{'、'.join(sorted(answer_tags))}）")

    # 名字相似度
    score, hit_name = best_match_names(guess_info, answer_info)
    if score >= 0.85:
        lines.append(f"{GREEN} 名字非常接近「{hit_name}」！")
    elif score >= 0.6:
        lines.append(f"{YELLOW} 名字有点接近「{hit_name}」？")

    # 判定命中：必须是“同一个角色”——名字完全一致，或唯一 id 一致。
    # 出演作品重合只作线索反馈；同系列共演角色（如 Fate 的远坂凛/阿尔托莉雅）
    # 即使作品/类型/标签大量重合，也不再算答对。
    exact = score >= 1.0
    same_id = str(guess_info.get("id")) == str(answer_info.get("id"))
    hit = exact or same_id
    return lines, hit


# ---------- 渐进线索 ----------

def build_hints(answer_info, max_hints=None):
    """生成渐进线索列表，难度从模糊到清晰。max_hints 缺省时从 settings 读取。"""
    if max_hints is None:
        import settings
        max_hints = settings.HINTS_TOTAL
    hints = []
    n_works = len(answer_info.get("works") or [])
    hints.append(f"这个角色出演过 {n_works} 部（主角/配角）作品")

    years = answer_info.get("years") or []
    if years:
        hints.append(f"最早 {min(years)} 年登场，最近活跃到 {max(years)} 年")
    else:
        hints.append("该角色没有可考的登场年代")

    rating = answer_info.get("highest_rating") or 0
    hints.append(f"出演作品最高评分 {rating:.1f}" if rating else "评分数据缺失")

    tags = answer_info.get("tags") or []
    hints.append(f"来源标签：{'、'.join(tags) if tags else '无数据'}")

    types = sorted({w.get("type") for w in answer_info.get("works") or []} - {None})
    hints.append(f"作品类型：{'、'.join(types) if types else '未知'}")

    cvs = answer_info.get("cvs") or []
    if cvs:
        hints.append(f"CV：{'、'.join(cvs[:3])}" + (" 等" if len(cvs) > 3 else ""))
    else:
        hints.append("CV 数据缺失")

    works = answer_info.get("works") or []
    if works:
        names = "、".join([f"《{w.get('name', '')}》" for w in works[:3]])
        hints.append(f"出演作品：{names}" + (" 等" if len(works) > 3 else ""))

    return hints[:max_hints]


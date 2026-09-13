"""
Bangumi v0 API 统一客户端
=========================
将所有 HTTP 通信、API 源切换与图片 URL 改写集中到一个客户端实例。

设计动机（v3.4.0 重构）：
- 原先 bangumi.py 和 main.py 各有一套 HTTP 重试/超时/UA 逻辑，代码重复且易不一致
- 图片 URL 在数据拉取时就被改写并落盘（wife_db.json / clue_cache.json），
  切换 API 源后旧缓存里的图片域名不会更新，导致下载失败
- API_SOURCE 的检查散落在 settings.api_base()、bangumi.rewrite_image_url()、
  main._api_fail_hint() 三处，任何一处遗漏都会造成不一致

重构后：
- 客户端单例统一管理 HTTP（GET/POST/下载）、API 源、UA 和图片 URL 改写
- 图片 URL 全程原始存储（lain.bgm.tv），仅在下载时按当前 API 源动态改写
- 支持双向改写：切回 official 时自动把旧缓存的 bgmimg.anibt.net 还原为 lain.bgm.tv
- 切换 API 源只需调 client.refresh()，后续请求自动走新源
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import settings

VERSION = "3.4.0"


class BangumiClient:
    """Bangumi v0 API 统一客户端（单例）。

    所有模块通过模块级 ``client`` 实例调用 API。
    ``settings.load()`` 后，下一次请求会自动检测 API_SOURCE 变化并刷新；
    也可手动调用 ``client.refresh()`` 立即生效。
    """

    _instance = None

    # API 技术参数（不可配置）
    SEARCH_LIMIT_MAX = 25   # v0 search 接口 limit 的保守上限
    BATCH_SIZE = 10         # search 每次批量取的条数

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._source = None
            cls._instance._base_url = None
            cls._instance._ua = None
            cls._instance._refresh()
        return cls._instance

    # ---- 配置刷新 ----

    def refresh(self):
        """从 settings 重新读取配置（API 源切换后调用）。"""
        self._refresh()

    def _refresh(self):
        self._source = (settings.API_SOURCE or "official").strip()
        self._base_url = settings.api_base()
        self._ua = {
            "User-Agent": f"astrbot-plugin-my-waifu/{VERSION} "
                          f"(https://github.com/SR3MP/astrbot_plugin_my_waifu)"
        }

    def _ensure_fresh(self):
        """每次访问前检查 API_SOURCE 是否变化，变了则自动刷新。"""
        current = (settings.API_SOURCE or "official").strip()
        if current != self._source:
            self._refresh()

    # ---- 属性 ----

    @property
    def source(self):
        """当前 API 源标识（official/mirror/自定义URL）。"""
        self._ensure_fresh()
        return self._source

    @property
    def base_url(self):
        """API 基地址（带 /v0 后缀）。"""
        self._ensure_fresh()
        return self._base_url

    @property
    def ua(self):
        """User-Agent 字典（供外部模块复用）。"""
        self._ensure_fresh()
        return self._ua

    # ---- 图片 URL 改写（双向） ----

    def rewrite_image_url(self, url):
        """根据当前 API 源改写图片 URL。

        - official: lain.bgm.tv 原样保留；同时把旧缓存的 bgmimg.anibt.net 还原
        - mirror:   lain.bgm.tv → bgmimg.anibt.net
        - 自定义:   不改写（反代方自行处理图片域名）

        双向改写确保：即使旧缓存（wife_db.json / clue_cache.json）里存的是
        镜像域名，切回 official 后也能自动还原为官方域名。
        """
        if not url:
            return url
        src = self.source
        if src == "mirror":
            return url.replace("lain.bgm.tv", "bgmimg.anibt.net")
        if src == "official":
            return url.replace("bgmimg.anibt.net", "lain.bgm.tv")
        return url

    # ---- HTTP 基础 ----

    def _urlopen(self, req, timeout):
        """带重试的请求：指数退避，网络波动/限流自动重试。

        超时只依赖 urlopen 的 timeout 参数（作用于连接+读取），
        不触碰 socket.setdefaulttimeout —— 那是进程级全局状态，
        多线程并发设置/恢复会互相覆盖。
        """
        last = None
        for attempt in range(settings.RETRY_TIMES + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.HTTPError, urllib.error.URLError,
                    socket.timeout, TimeoutError, ConnectionError) as e:
                last = e
                if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                    break  # 非限流/非5xx错误不重试
                if attempt < settings.RETRY_TIMES:
                    time.sleep(settings.RETRY_BASE * (2 ** attempt))
        raise last

    def get(self, path):
        """GET /v0/{path}，返回解析后的 JSON。"""
        req = urllib.request.Request(f"{self.base_url}{path}", headers=self.ua)
        return self._urlopen(req, settings.HTTP_TIMEOUT)

    def post(self, path, body):
        """POST /v0/{path}，返回解析后的 JSON。"""
        data = json.dumps(body).encode("utf-8")
        headers = {**self.ua, "Content-Type": "application/json"}
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method="POST"
        )
        return self._urlopen(req, settings.HTTP_TIMEOUT)

    def download(self, url, dest_path):
        """下载文件到 dest_path，成功返回 True。

        - 图片 URL 自动按当前 API 源改写（调用方无需预处理）
        - 先写临时文件再 os.replace 原子替换，避免中断留下半截文件
        - 校验文件大小 > 0，空文件视为失败
        - 限流/5xx/超时按指数退避重试；非限流 4xx 不重试
        """
        dest_path = Path(dest_path)
        url = self.rewrite_image_url(url)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        req = urllib.request.Request(url, headers=self.ua)
        last = None
        for attempt in range(settings.RETRY_TIMES + 1):
            try:
                with urllib.request.urlopen(req, timeout=settings.IMAGE_HTTP_TIMEOUT) as r, \
                        open(tmp_path, "wb") as f:
                    f.write(r.read())
                if tmp_path.stat().st_size <= 0:
                    raise OSError("downloaded empty image")
                os.replace(tmp_path, dest_path)
                return True
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, ConnectionError, OSError) as e:
                last = e
                if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                    break
                if attempt < settings.RETRY_TIMES:
                    time.sleep(settings.RETRY_BASE * (2 ** attempt))
        # 清理残留临时文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise last

    # ---- API 端点 ----

    def search_subjects(self, subject_type, start_year, end_year,
                        limit=None, offset=0):
        """按热度排序搜索作品。

        subject_type: [2] / [1,2,4,6] 等 Bangumi 大类
        日期上界用当前时间截断，避免抽到尚未播出的作品。
        """
        from datetime import datetime
        today = datetime.now()
        end_date = min(datetime(end_year + 1, 1, 1), today).strftime("%Y-%m-%d")
        body = {
            "sort": "heat",
            "filter": {
                "type": subject_type,
                "air_date": [f">={start_year}-01-01", f"<{end_date}"],
            },
        }
        batch = limit or self.BATCH_SIZE
        return self.post(f"/search/subjects?limit={batch}&offset={offset}", body)

    def get_subject_characters(self, subject_id):
        """GET /v0/subjects/{id}/characters"""
        return self.get(f"/subjects/{subject_id}/characters")

    def get_character_detail(self, character_id):
        """GET /v0/characters/{id}"""
        return self.get(f"/characters/{character_id}")

    def get_character_subjects(self, character_id):
        """GET /v0/characters/{id}/subjects"""
        return self.get(f"/characters/{character_id}/subjects")

    def get_subject_detail(self, subject_id):
        """GET /v0/subjects/{id}"""
        return self.get(f"/subjects/{subject_id}")

    def get_character_persons(self, character_id):
        """GET /v0/characters/{id}/persons（CV）"""
        return self.get(f"/characters/{character_id}/persons")

    def search_characters(self, keyword, limit=10, offset=0):
        """POST /v0/search/characters，按名字搜角色。

        limit 钳制在 1~SEARCH_LIMIT_MAX，避免超过 API 上限被服务端拒绝。
        """
        limit = max(1, min(int(limit), self.SEARCH_LIMIT_MAX))
        return self.post(
            f"/search/characters?limit={limit}&offset={offset}",
            {"keyword": keyword.strip()},
        )


# 模块级单例：所有模块共享同一个客户端实例
client = BangumiClient()

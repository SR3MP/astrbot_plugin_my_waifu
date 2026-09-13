# 更新日志

## v3.4.0 — 2026-09-13

### 重构

1. **新增 `api_client.py` — 统一 Bangumi API 客户端（核心重构）**
   - **动机**：原先 `bangumi.py` 和 `main.py` 各有一套 HTTP 重试 / 超时 / UA 逻辑，代码重复且易不一致；图片 URL 在数据拉取时就被改写并落盘，切换 API 源后旧缓存里的图片域名不会更新，导致下载失败；`API_SOURCE` 的检查散落在 `settings.api_base()`、`bangumi.rewrite_image_url()`、`main._api_fail_hint()` 三处。
   - **重构后**：`BangumiClient` 单例统一管理 HTTP（GET/POST/下载）、API 源切换、UA 和图片 URL 改写。所有模块通过 `from api_client import client` 调用 API。`settings.load()` 后下一次请求自动检测 `API_SOURCE` 变化并刷新，也可手动调 `client.refresh()` 立即生效。

2. **图片 URL 原始存储 + 动态改写**
   - **问题**：v3.3.0 镜像源在拉取数据时即改写图片 URL 并落盘，切换 API 源后 `wife_db.json` / `clue_cache.json` 中的旧 URL 不会更新，导致切回 official 后图片下载失败。
   - **修复**：图片 URL 全程以原始域名（lain.bgm.tv）存储，仅在下载时按当前数据源动态改写。支持双向改写：切回 official 时自动把旧缓存的 bgmimg.anibt.net 还原为 lain.bgm.tv。

### Bug 修复

1. **`bangumi.py` — 未使用的 `datetime` 导入（pyflakes 警告）**
   - **问题**：`from datetime import date, datetime` 中 `datetime` 从未使用（v3.2.1 修复 `year_of_subject` 变量名遮蔽时遗留）。
   - **修复**：改为 `from datetime import date`。

2. **`guess.py` — docstring 线索顺序描述与实现不符**
   - **问题**：模块 docstring 中线索顺序写为「3作品类型 4CV 5作品名 6立绘」，但实际实现为 3来源标签 4作品类型 5CV 6出演作品名。
   - **修复**：更正为「3来源标签 4作品类型 5CV 6出演作品名」。

3. **`main.py` `_help_text()` — 使用已废弃的 `settings.API_SOURCE`（一致性 Bug）**
   - **问题**：`_help_text()` 中显示当前数据源时直接引用 `settings.API_SOURCE`，而 `_api_fail_hint()` 已改用 `client.source`。两者不一致，且 `settings.API_SOURCE` 在面板配置加载前可能未及时更新。
   - **修复**：统一改为 `client.source`。

## v3.2.2 — 2026-09-13

### Bug 修复

1. **`main.py` `_guess_personal` — 0 字节坏图片发给用户（功能性 Bug）**
   - **问题**：猜对老婆后展示立绘时，图片检查仅用 `if not img_path.exists()`，不验证文件大小。若本地存在 0 字节的坏文件（历史遗留的失败下载、磁盘异常等），会跳过重新下载，直接把 0 字节图片发给用户，导致图片渲染失败或显示破损图标。对比同文件 `on_portrait` 方法使用了 `img_path.exists() and img_path.stat().st_size > 0` 双重检查，此处遗漏了大小校验。
   - **修复**：与 `on_portrait` 统一，改为 `if not (img_path.exists() and img_path.stat().st_size > 0)` 触发重新下载，并在发送前二次校验 `if img_path and img_path.exists() and img_path.stat().st_size > 0`。

2. **全文件版本号不一致**
   - **问题**：`@register` 装饰器和 UA 字符串停留在 3.2.0，`main.py` 文档字符串停留在 v3.2，而 `metadata.yaml`/`README.md` 已是 3.2.1。版本号跨文件不一致，影响 AstrBot 插件管理页面的版本展示和 Bangumi API 请求标识。
   - **修复**：统一所有版本号为 3.2.2（`main.py` `@register`、UA、文档字符串、`bangumi.py` UA、`metadata.yaml`、`README.md`）。

## v3.2.1 — 2026-09-13

### Bug 修复

1. **`settings.py` — SUBJECT_TYPES 字符串回退缺陷（潜在功能性 Bug）**
   - **问题**：当 WebUI 配置面板返回的作品类型列表包含字符串值（如 `["2", "4"]`）时，原代码先过滤出 `int` 类型元素，若全部被过滤掉则回退为原始 `types` 列表（保留字符串）。这导致后续 `s.get("type") not in SUBJECT_TYPES` 做 int vs str 比较，结果恒为 True，所有作品被跳过，角色线索聚合返回空数据。
   - **修复**：改为遍历所有元素逐一 `int()` 转换，跳过非法值；若全部转换失败则回退到默认 `[2]`，不再保留字符串。

2. **`settings.py` — SOLVED_BONUS 加载死代码**
   - **问题**：`_as_int()` 永不返回 `None`（非法值时返回 `default=0`），但加载逻辑中写了 `if iv is not None:` 守卫，属于永真条件，代码冗余且有误导性。
   - **修复**：移除无意义的 `if` 守卫，直接赋值。

3. **`bangumi.py` — `year_of_subject` 变量名遮蔽**
   - **问题**：函数内局部变量 `date` 遮蔽了模块级 `from datetime import date` 导入，虽然当前不影响运行结果，但属于代码隐患，后续维护若在该函数内使用 `date` 类会出错。
   - **修复**：重命名局部变量为 `air_date`，消除遮蔽。

4. **`bangumi.py` — `random_wife` 未使用异常变量**
   - **问题**：`except Exception as e:` 中 `e` 未被使用（pyflakes 警告）。
   - **修复**：改为 `except Exception:`。

5. **`harem.py` — `_load` 未使用异常变量**
   - **问题**：`except Exception as e:` 中 `e` 未被使用（pyflakes 警告）。
   - **修复**：改为 `except Exception:`。

6. **`local_db.py` — 未使用导入 `timedelta`**
   - **问题**：`from datetime import date, timedelta` 中 `timedelta` 从未使用（pyflakes 警告）。
   - **修复**：移除 `timedelta`。

7. **`local_db.py` — 未使用变量 `shown`**
   - **问题**：`build_local_db` 中 `shown = 0` 声明后从未读取或修改（pyflakes 警告）。
   - **修复**：移除该变量。

8. **`main.py` — 无占位符的 f-string**
   - **问题**：`f"  · 每天可无限猜，猜错不扣分"` 不含任何 `{}` 占位符，`f` 前缀多余（pyflakes 警告）。
   - **修复**：移除 `f` 前缀。

### 代码质量

- pyflakes 零警告通过
- 全部文件语法编译验证通过
- `SUBJECT_TYPES` 字符串转换逻辑经单元测试验证

"""统一数据目录（符合 AstrBot 上架规范）。

所有持久化数据必须写到 AstrBot 提供的
``data/plugin_data/<plugin_name>`` 下，而不是插件自身目录，
以便用户迁移、备份和审计。插件自身目录只保留代码与静态资源。

本模块在导入时完成一次性旧数据迁移（幂等）：
旧版本把数据写在插件目录的 data/ 与 images/ 下，升级后自动搬到新目录。
迁移采用「先复制校验、再改名旧文件」的方式；目标已存在时不覆盖，
确保即使中途失败也不丢用户数据。
"""
import json
import os
import shutil
import sys
from pathlib import Path

try:
    from astrbot.api.star import StarTools

    _PLUGIN_NAME = "astrbot_plugin_my_waifu"
    DATA_DIR = Path(StarTools.get_data_dir(_PLUGIN_NAME)).resolve()
except Exception:  # 非 AstrBot 环境（本地命令行同步脚本）回退到插件目录
    DATA_DIR = (Path(__file__).resolve().parent / "plugin_data").resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_DIR = DATA_DIR / "images"
TEMP_DIR = DATA_DIR / "temp"

# 旧版本插件目录（迁移来源）
_LEGACY_DIR = Path(__file__).resolve().parent
_LEGACY_DATA = _LEGACY_DIR / "data"
_LEGACY_IMAGES = _LEGACY_DIR / "images"
_LEGACY_YEAR_POOL = _LEGACY_DIR / ".year_pool_cache.json"

# 需要迁移的插件数据文件（不含 cmd_config.json / t2i_templates 等 AstrBot 框架管理的内容）
_MIGRATE_FILES = (
    ("user_profiles.json", DATA_DIR / "user_profiles.json"),
    ("wife_db.json", DATA_DIR / "wife_db.json"),
    ("clue_cache.json", DATA_DIR / "clue_cache.json"),
)


def _copy_if_newer(src: Path, dst: Path) -> bool:
    """目标不存在时复制；目标存在但旧文件更新时复制覆盖（保留较多数据的一方）。"""
    if not src.is_file():
        return False
    if dst.exists():
        # 已迁移过：只有当旧文件明显更新（用户在新版使用前又产生了数据）才补迁
        try:
            if src.stat().st_mtime <= dst.stat().st_mtime:
                return False
        except OSError:
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".migrate_tmp")
    try:
        shutil.copy2(str(src), str(tmp))
        os.replace(str(tmp), str(dst))
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def migrate_legacy_data():
    """把旧插件目录的用户数据迁移到新数据目录，返回迁移说明列表。

    幂等：可重复调用；目标已存在且不比旧文件旧时跳过。
    图片目录数量大（上百张立绘），用「目标不存在才复制」避免逐张覆盖。
    """
    notes = []
    for name, dst in _MIGRATE_FILES:
        src = _LEGACY_DATA / name
        if _copy_if_newer(src, dst):
            notes.append(f"{name} -> {dst.name}")
    # 年份池缓存（3.8MB）
    if _copy_if_newer(_LEGACY_YEAR_POOL, DATA_DIR / ".year_pool_cache.json"):
        notes.append(".year_pool_cache.json")
    # 立绘：仅补迁目标缺失的文件
    if _LEGACY_IMAGES.is_dir():
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in _LEGACY_IMAGES.iterdir():
            if f.is_file() and not (IMAGE_DIR / f.name).exists():
                try:
                    shutil.copy2(str(f), str(IMAGE_DIR / f.name))
                    copied += 1
                except OSError:
                    pass
        if copied:
            notes.append(f"images/{copied} 张立绘")
    return notes


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def legacy_paths_exist() -> bool:
    """是否还存在旧目录数据（供运维确认迁移是否完成）。"""
    return any((_LEGACY_DATA / n).exists() for n, _ in _MIGRATE_FILES) or _LEGACY_YEAR_POOL.exists()

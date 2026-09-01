"""
自动备份与启动恢复 —— 与启动对账配合，形成数据兜底闭环

启动对账能发现"账本丢了"，但它只负责暂停、不负责修复。
本模块负责让数据**能被找回来**：

  定时导出  →  backups/bot_YYYYMMDD_HHMMSS.json
  启动检测  →  DB 缺失/为空 且 存在备份 → 自动恢复并告警

为什么不用平台的备份：
  Render 免费层临时盘、Serv00 停机、容器重建都会让本地文件消失；
  平台备份（alwaysdata 3 天、Render 无）既不可控也覆盖不到容器内写入。

保留策略：滚动保留最近 keep 份（默认 7），避免把小磁盘撑满。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from datetime import datetime, timezone, timedelta

from config import logger

CST = timezone(timedelta(hours=8))

# 备份目录：与数据库同级的 backups/
BACKUP_DIR = "backups"
KEEP_DEFAULT = 7


class BackupManager:
    def __init__(self, db_file_getter, keep: int = KEEP_DEFAULT,
                 uploader=None, upload_every: int = 4):
        """
        uploader: 可选的异步回调 (path, data) -> bool
            用于把备份推到**本地盘之外**的地方。

            为什么必须有：Render / 免费容器的工作目录是临时盘，
            数据库和 backups/ 在同一个盘上 —— 数据库丢了的那次，
            本地备份也会同时消失，等于白备份。
            把备份发到 Telegram 这类外部存储，才是真正兜得住的底。

        upload_every: 每做几次本地备份推一次外部（默认 4，
            即 6h×4=24 小时推一次，避免刷屏）
        """
        # 用 getter 延迟读取，因为 storage.DB_FILE 在测试里会被改写
        self._db_file = db_file_getter
        self.keep = keep
        self.last_backup = 0.0
        self.interval = 6 * 3600      # 默认 6 小时
        self.enabled = True
        self.uploader = uploader
        self.upload_every = upload_every
        self._export_count = 0
        self.last_upload = 0.0
        self.backup_dir_volatile = False   # 外部存储不可用时置 True 并告警

    # ─────────── 目录 ───────────

    def _dir(self) -> str:
        db = self._db_file() or "bot.db"
        return os.path.join(os.path.dirname(os.path.abspath(db)) or ".", BACKUP_DIR)

    # ─────────── 导出 ───────────

    async def export(self) -> str:
        """导出一份 JSON 备份，返回文件路径；失败返回空串"""
        if not self.enabled:
            return ""
        try:
            import storage
            data = await storage.export_db_to_json()
            if not data:
                return ""
            d = self._dir()
            os.makedirs(d, exist_ok=True)
            # 时间戳精确到微秒：只到秒的话，同一秒内的多次导出会互相覆盖
            stamp = datetime.now(CST).strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(d, f"bot_{stamp}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            self.last_backup = datetime.now().timestamp()
            self._rotate()
            logger.info(f"💾 已备份数据库 → {os.path.basename(path)}")

            # 推送到外部存储（本地盘靠不住）
            await self._maybe_upload(path, data)
            return path
        except Exception as e:
            logger.warning(f"自动备份失败: {e}")
            return ""

    async def _maybe_upload(self, path: str, data: str):
        """
        按节流频率把备份推到外部。

        没有任何外部通道时明确告警一次 —— 静默失败最危险，
        用户会以为有备份，实际盘一清就全没了。
        """
        self._export_count += 1
        if self.uploader is None:
            if not self.backup_dir_volatile:
                self.backup_dir_volatile = True
                logger.warning(
                    "⚠️ 未配置备份上传通道：备份只存在本地盘。"
                    "若运行在临时盘环境(Render 免费层等)，"
                    "数据库丢失时备份会一同消失。建议配置 uploader。")
            return

        if self._export_count % self.upload_every != 0:
            return

        try:
            ok = await self.uploader(path, data)
            if ok:
                self.last_upload = datetime.now().timestamp()
                logger.info(f"☁️ 备份已推送到外部存储 → {os.path.basename(path)}")
            else:
                logger.warning("备份外部推送返回失败")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"备份外部推送异常: {type(e).__name__}: {e}")

    def _rotate(self):
        """滚动删除旧备份，只保留最近 keep 份"""
        try:
            d = self._dir()
            files = sorted(glob.glob(os.path.join(d, "bot_*.json")))
            for f in files[:-self.keep]:
                try:
                    os.unlink(f)
                except OSError:
                    pass
        except Exception:
            pass

    # ─────────── 恢复 ───────────

    def _latest(self) -> str:
        files = sorted(glob.glob(os.path.join(self._dir(), "bot_*.json")))
        return files[-1] if files else ""

    async def db_is_empty(self) -> bool:
        """
        判断数据库是否"没有有价值的数据"。

        不能用文件大小判断 —— init_db() 建完表就有几 KB，
        load_and_init() 又会写入默认配置，文件永远不"小"。
        真正的判据是内容：既没有运行时状态（持仓），也没有用户配置。
        """
        db = self._db_file() or "bot.db"
        try:
            if not os.path.exists(db):
                return True
        except OSError:
            return True

        try:
            import storage
            # 只以 runtime_state 为判据：
            # 它只在机器人真正运行、保存过状态时才有内容，是
            # "这台机器是否跑过"的可靠信号。
            #
            # 不能用 load_config() —— 它第一行就是 dict(DEFAULT_CONFIG)，
            # 无论表里有没有数据都返回非空，拿它判断必然失效。
            state = await storage.load_runtime_state()
            return not bool(state)
        except Exception as e:
            logger.warning(f"检查数据库内容失败，按空库处理: {e}")
            return True

    async def restore_if_needed(self) -> tuple:
        """
        启动时调用。若数据库缺失/为空而存在备份，则自动恢复。

        返回 (restored: bool, message: str)
        """
        if not await self.db_is_empty():
            return False, ""

        latest = self._latest()
        if not latest:
            return False, ("数据库为空且无备份文件 —— "
                           "首次启动属正常；若非首次，请检查数据丢失原因")

        try:
            import storage
            with open(latest, "r", encoding="utf-8") as f:
                data = f.read()
            ok = await storage.import_db_from_json(data)
            if ok:
                msg = (f"检测到数据库为空，已从备份自动恢复：{os.path.basename(latest)}\n"
                       f"恢复后请务必用 /reconcile 核对持仓")
                logger.warning(f"♻️ {msg}")
                return True, msg
            return False, f"找到备份 {os.path.basename(latest)} 但恢复失败"
        except Exception as e:
            return False, f"恢复备份异常: {type(e).__name__}: {e}"

    # ─────────── 定时循环 ───────────

    async def loop(self, stop_event: asyncio.Event):
        """后台定时备份，可由 stop_event 终止"""
        await asyncio.sleep(60)        # 启动 1 分钟后做第一次
        while not stop_event.is_set():
            try:
                await self.export()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"备份循环异常: {e}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

"""
状态持久化。

两件事必须做对，否则会丢钱：
  1. 原子写入 —— 写一半崩了不能留下坏文件
  2. 异地备份 —— Render 免费层磁盘不持久，重启即丢
"""
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def load(path: str, default: dict) -> dict:
    """读状态。文件损坏时返回 default 并告警（不静默吞掉）。"""
    if not os.path.exists(path):
        return dict(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("根节点不是对象")
        return data
    except Exception as e:
        logger.error(f"状态文件损坏，已重置: {e}")
        # 留一个副本供人工排查
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        return dict(default)


def save(path: str, data: dict) -> bool:
    """原子写入。失败返回 False，由调用方告警（不能静默）。"""
    try:
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"状态保存失败: {e}")
        return False


def to_backup_bytes(data: dict) -> bytes:
    """生成可发到 Telegram 的备份文件（异地容灾）"""
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def empty_state() -> dict:
    """初始状态。coins 为空，运行时按需填充。"""
    return {
        "version": 2,
        "running": True,
        "coins": {},        # sym -> {center, spacing, levels, lots, orders}
        "risk": {
            "peak_equity": 0.0,
            "day_start_equity": 0.0,
            "day_start_date": "",
            "realized_pnl": 0.0,
            "retired": False,
        },
        "params": {},
    }

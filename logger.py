"""日志配置模块：按天轮转日志文件，保留最近 3 天历史。"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

# 默认日志目录（与需求一致）
DEFAULT_LOG_DIR = r"C:\ProgramData\BLEAutoUnlock\logs"

# 防止同一进程内重复添加 handler
_configured = False

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_log_dir(log_dir: str | None) -> str:
    """确定可写的日志目录；默认目录不可写时回退到用户目录/临时目录。"""
    candidates = []
    if log_dir:
        candidates.append(log_dir)
    else:
        candidates.append(DEFAULT_LOG_DIR)
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            candidates.append(os.path.join(local_appdata, "BLEAutoUnlock", "logs"))
    candidates.append(os.path.join(os.environ.get("TEMP", "."), "BLEAutoUnlock", "logs"))

    last_error: OSError | None = None
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError as exc:
            last_error = exc
    # 所有候选目录都不可写时，改用当前目录（仍失败则向上抛出）
    fallback = os.path.join(os.getcwd(), "logs")
    os.makedirs(fallback, exist_ok=True)
    if last_error is not None:
        logging.getLogger("ble_autounlock").warning(
            "日志目录不可写已回退到 %s（原目录 %s: %s）",
            fallback, candidates[0], last_error,
        )
    return fallback


def setup_logging(level: str = "INFO",
                  log_dir: str | None = None,
                  console: bool = True) -> logging.Logger:
    """初始化根日志记录器：文件轮转 + 可选控制台输出。

    :param level: 日志级别（如 INFO / DEBUG）
    :param log_dir: 日志目录，默认 C:\\ProgramData\\BLEAutoUnlock\\logs
    :param console: 是否同时输出到控制台（前台调试模式建议开启）
    """
    global _configured
    level_num = getattr(logging, str(level).upper(), logging.INFO)
    log_dir = _resolve_log_dir(log_dir)

    log_file = os.path.join(log_dir, "ble_autounlock.log")
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level_num)

    if not _configured:
        # 按天轮转；backupCount=3 表示保留最近 3 天的历史日志文件
        file_handler = TimedRotatingFileHandler(
            log_file, when="midnight", backupCount=3, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

        if console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            root.addHandler(console_handler)
        _configured = True

    return logging.getLogger("ble_autounlock")

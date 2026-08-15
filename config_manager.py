"""配置管理模块：负责 config.json 的读写与 Windows DPAPI 密码加解密。

密码绝不落盘明文：使用当前 Windows 用户的 DPAPI 凭据加密后，
以 base64 字符串保存在 config.json 的 windows_password 字段中。

注意：DPAPI 加密与当前用户绑定，若以其他账户（如 LocalSystem）
运行服务，将无法解密该密码，需在服务账户下重新执行 --set-password。
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 默认配置项；config.json 中缺失的字段会自动使用这里的默认值，
# 因此新增配置项不会导致旧配置文件失效。
DEFAULT_CONFIG: Dict[str, Any] = {
    # 设备类型：ios（IRK 随机地址）或 android（固定 MAC，预留）
    "device_type": "ios",
    # 64 位十六进制 IRK 密钥（32 个十六进制字符）
    "irk_key": "",
    # 命名 IRK 列表，可同时管理多台 iOS 设备（如 iPhone、iPad）。
    # 元素格式: {"name": "设备名", "key": "64位十六进制IRK"}；
    # 为空列表时回退使用上面的 irk_key 单密钥配置
    "irk_keys": [],
    # Android 固定 MAC 地址（预留）
    "android_mac": "",
    # DPAPI 加密后的 Windows 登录密码（base64），绝不明文保存
    "windows_password": "",
    # RSSI 高于该值视为“靠近”，持续 unlock_hold_seconds 秒后触发解锁
    "unlock_rssi": -60,
    # RSSI 低于该值或设备消失视为“离开”
    "lock_rssi": -75,
    # 两次扫描的间隔（秒）
    "scan_interval": 2,
    # 单次扫描持续时长（秒）
    "scan_duration": 2,
    # 设备连续多少次扫描未出现才考虑锁定
    "miss_count_before_lock": 5,
    # 设备消失后累计经过的最小时间（秒），用于误判防御
    "lock_min_elapsed": 10,
    # RSSI 持续达标的最短时间（秒）
    "unlock_hold_seconds": 3,
    # 蓝牙适配器异常时重置并重试的最大次数
    "adapter_reset_retries": 3,
    # 重置适配器后的延迟（秒）
    "adapter_reset_delay": 5,
    # IRK 解析方法：ble_standard（BLE 标准 AES-128，推荐）/
    #               legacy_hmac（需求中的 HMAC 变体）
    "irk_resolve_method": "ble_standard",
    # prand 在随机地址中的位置：head（前 3 字节，BLE 标准 + bleak 大端序
    # MAC 字符串）/ tail（后 3 字节，需求伪代码 random_address[3:6] 的
    # 线上字节序）；留空表示按解析方法自动选择（ble_standard=head,
    # legacy_hmac=tail）
    "irk_prand_position": "",
    "log_level": "INFO",
    "log_dir": r"C:\ProgramData\BLEAutoUnlock\logs",
}


class ConfigError(Exception):
    """配置相关错误。"""


def default_config_path() -> str:
    """返回默认配置文件路径。

    - 源码运行：与 main.py 同目录的 config.json
    - 打包 exe 运行：%APPDATA%\\BLEAutoUnlock\\config.json
      （避免写入 exe 所在目录失败，例如装在 Program Files 下）
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "BLEAutoUnlock", "config.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class ConfigManager:
    """配置管理器：加载、保存、按需加密/解密密码。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_config_path()
        self._data: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)

    # ------------------------------------------------------------- 文件读写

    def load(self) -> "ConfigManager":
        """从磁盘加载配置；文件不存在时自动创建默认配置。"""
        if not os.path.exists(self.path):
            logger.info("配置文件不存在，创建默认配置: %s", self.path)
            self.save()
            return self
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                user_config = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"读取配置文件失败 {self.path}: {exc}") from exc
        if not isinstance(user_config, dict):
            raise ConfigError(f"配置文件格式错误（应为 JSON 对象）: {self.path}")
        # 以默认配置为底，用户配置覆盖，保证新增字段自动补齐
        merged = copy.deepcopy(DEFAULT_CONFIG)
        merged.update(user_config)
        self._data = merged
        return self

    def save(self) -> None:
        """把当前配置写回磁盘（windows_password 保持加密状态）。"""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            raise ConfigError(f"保存配置文件失败 {self.path}: {exc}") from exc
        logger.info("配置已保存: %s", self.path)

    # ------------------------------------------------------------- 字段访问

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    # ------------------------------------------------------------- 密码加解密

    @staticmethod
    def encrypt_password(plaintext: str) -> str:
        """使用当前用户 DPAPI 加密明文密码，返回 base64 字符串。"""
        try:
            import win32crypt
        except ImportError as exc:
            raise ConfigError("缺少 pywin32，无法使用 DPAPI 加密密码") from exc
        blob, _description = win32crypt.CryptProtectData(
            plaintext.encode("utf-8"),
            "BLEAutoUnlock",
            None, None, None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN：禁止弹出任何 UI
        )
        return base64.b64encode(blob).decode("ascii")

    @staticmethod
    def decrypt_password(encrypted_b64: str) -> str:
        """使用当前用户 DPAPI 解密密码。"""
        try:
            import win32crypt
        except ImportError as exc:
            raise ConfigError("缺少 pywin32，无法使用 DPAPI 解密密码") from exc
        try:
            blob = base64.b64decode(encrypted_b64)
        except (ValueError, TypeError) as exc:
            raise ConfigError("windows_password 不是合法的 base64 数据") from exc
        try:
            data, _description = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        except Exception as exc:
            raise ConfigError(
                "DPAPI 解密失败（可能更换了 Windows 用户或用户凭据）",
            ) from exc
        return data.decode("utf-8")

    def set_password_encrypted(self, plaintext: str) -> None:
        """加密并保存密码（调用后仍需 save() 才会落盘）。"""
        if not plaintext:
            raise ConfigError("密码不能为空")
        self._data["windows_password"] = self.encrypt_password(plaintext)

    def get_password_plaintext(self) -> str:
        """返回明文密码；未配置时返回空字符串。"""
        encrypted = self._data.get("windows_password") or ""
        if not encrypted:
            return ""
        return self.decrypt_password(encrypted)

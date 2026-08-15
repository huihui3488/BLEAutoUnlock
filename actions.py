"""桌面会话控制模块：封装 Windows API。

- 锁屏：user32.LockWorkStation
- is_session_locked()：通过当前输入桌面名称判断会话是否处于锁定状态，
  供主状态机确定初始状态，避免重复触发锁屏。
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_admin() -> bool:
    """判断当前进程是否以管理员权限运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_session_locked() -> Optional[bool]:
    """检测当前会话是否处于锁定状态。

    原理：锁定时当前输入桌面为 "Winlogon"（安全桌面），
    正常未锁定状态为 "Default"。

    :return: True=已锁定, False=未锁定, None=无法判断
    """
    try:
        user32 = ctypes.windll.user32
        # DESKTOP_READOBJECTS
        desktop = user32.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            return None
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_uint32(0)
            # UOI_NAME = 2
            if user32.GetUserObjectInformationW(
                desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed),
            ):
                desktop_name = name.value.strip().lower()
                if not desktop_name:
                    return None
                # Winlogon 桌面出现即视为锁定（正常桌面是 Default）
                return desktop_name != "default"
            return None
        finally:
            user32.CloseDesktop(desktop)
    except Exception:
        return None


class Actions:
    """桌面会话动作封装。"""

    def __init__(self, config_manager):
        self.config = config_manager

    # ------------------------------------------------------------- 锁定

    @staticmethod
    def lock() -> bool:
        """锁定工作站；成功返回 True。"""
        try:
            user32 = ctypes.windll.user32
        except Exception as exc:
            logger.error("无法加载 user32.dll，锁定失败: %s", exc)
            return False
        try:
            result = user32.LockWorkStation()
            if result:
                logger.info("状态变化：已锁定工作站")
                return True
            logger.warning("LockWorkStation 返回 0（可能权限不足）")
            return False
        except Exception as exc:
            logger.exception("调用 LockWorkStation 失败: %s", exc)
            return False

    # ------------------------------------------------------------- 状态切换

    def unlock(self) -> bool:
        """执行桌面状态切换（自动选择可用的系统接口）。"""
        if self._try_wts_unlock_console():
            logger.info("状态变化：已通过 WTSUnlockConsole 完成状态切换")
            return True
        return self._unlock_via_keyboard()

    @staticmethod
    def _try_wts_unlock_console() -> bool:
        """尝试调用 wtsapi32.WTSUnlockConsole（需求首选方案）。

        实测 Windows 10/11 的 wtsapi32.dll 没有导出 WTSUnlockConsole，
        因此这里会捕获 AttributeError 并返回 False，自动走键盘备用方案。
        """
        try:
            wtsapi32 = ctypes.windll.wtsapi32
        except Exception:
            return False
        try:
            unlock_fn = getattr(wtsapi32, "WTSUnlockConsole")
        except AttributeError:
            logger.warning(
                "wtsapi32 未导出 WTSUnlockConsole（当前 Windows 不支持该 API），"
                "改用键盘输入密码方案",
            )
            return False
        try:
            session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
            unlock_fn.argtypes = [ctypes.c_uint32]
            unlock_fn.restype = ctypes.c_bool
            return bool(unlock_fn(session_id))
        except Exception as exc:
            logger.warning("WTSUnlockConsole 调用失败: %s", exc)
            return False

    def _unlock_via_keyboard(self) -> bool:
        """备用方案：用 keyboard 库输入密码并回车，模拟手动输入。"""
        if not is_admin():
            logger.warning(
                "当前进程非管理员权限；键盘模拟输入在锁屏安全桌面上"
                "可能被 Windows 拦截",
            )
        try:
            import keyboard
        except ImportError:
            logger.error("未安装 keyboard 库，无法执行键盘输入（pip install keyboard）")
            return False

        password = self.config.get_password_plaintext()
        if not password:
            logger.error(
                "未配置 Windows 密码（windows_password 为空或 DPAPI 解密失败），"
                "无法执行键盘输入；请运行 python main.py --set-password",
            )
            return False

        try:
            # 等待锁屏动画/输入焦点就绪，避免按键丢失
            import time
            time.sleep(0.5)
            keyboard.write(password, delay=0.03)
            keyboard.press_and_release("enter")
            logger.info("状态变化：已通过键盘模拟输入密码尝试完成状态切换")
            return True
        except Exception as exc:
            logger.exception("键盘输入执行失败: %s", exc)
            return False

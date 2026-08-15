"""程序入口：参数解析 + 主状态机。

用法：
  python main.py --run                 前台调试运行（带控制台日志）
  python main.py --install             安装为 Windows 服务（需管理员权限）
  python main.py --uninstall           卸载 Windows 服务（需管理员权限）
  python main.py --set-password        交互式加密保存 Windows 登录密码
  python main.py --selftest            运行自检（IRK 向量、配置、依赖、权限）
  python main.py --config PATH         指定配置文件路径（默认 main.py 同目录）

核心逻辑：
  - RSSI >= unlock_rssi 且持续 unlock_hold_seconds 秒  -> 解锁
  - RSSI < lock_rssi 或设备消失，连续 miss_count_before_lock 次扫描
    且累计超过 lock_min_elapsed 秒                      -> 锁定
  - 介于两者之间：保持当前状态（迟滞区间，避免抖动）
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib.util
import logging
import os
import sys
import threading
import time
from typing import Optional

from actions import Actions, is_admin, is_session_locked
from config_manager import ConfigManager, ConfigError
from logger import setup_logging
from resolver import ResolverError, create_resolver

logger = logging.getLogger(__name__)


# ================================================================ 单实例控制


class SingleInstance:
    """使用 win32event.CreateMutex 保证同一时间只有一个实例运行。"""

    MUTEX_NAME = "BLEAutoUnlock_SingleInstance"

    def __init__(self):
        self._mutex = None
        self._already_exists = False
        try:
            import win32api
            import win32event
        except ImportError:
            logger.warning("缺少 pywin32，跳过单实例互斥检查")
            return
        # pywin32 的错误码常量定义在 winerror 模块（不在 win32event 中）
        try:
            from winerror import ERROR_ALREADY_EXISTS
        except ImportError:
            ERROR_ALREADY_EXISTS = 183  # Windows 错误码：ERROR_ALREADY_EXISTS
        # 创建互斥体；若已存在则说明已有实例在运行
        self._mutex = win32event.CreateMutex(None, False, self.MUTEX_NAME)
        self._already_exists = (
            win32api.GetLastError() == ERROR_ALREADY_EXISTS
        )

    @property
    def already_running(self) -> bool:
        return bool(self._mutex is not None and self._already_exists)

    def close(self) -> None:
        """释放互斥体句柄。"""
        if self._mutex is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._mutex)
            except Exception:
                pass
            self._mutex = None


# ================================================================ 电源唤醒监听


class PowerMonitor:
    """监听 Windows 电源广播消息（WM_POWERBROADCAST），唤醒后触发回调。

    使用 pywin32 的 win32gui 在隐藏窗口中处理消息；若 pywin32 不可用，
    则退化为依赖主循环自身的异常重连（睡眠唤醒后的扫描异常会自动恢复）。
    """

    WM_POWERBROADCAST = 0x0218
    PBT_APMRESUMEAUTOMATIC = 0x0012  # 系统自动恢复运行（正常唤醒）
    PBT_APMRESUMECRITICAL = 0x0018   # 关键恢复（电池耗尽等）

    def __init__(self, on_resume):
        self._on_resume = on_resume
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._hwnd = None

    def start(self) -> None:
        """启动消息循环线程（不可用时记录警告并继续）。"""
        try:
            import win32gui  # noqa: F401
        except ImportError:
            logger.warning(
                "缺少 pywin32，电源唤醒监听不可用；"
                "主循环的异常重连机制仍可恢复扫描",
            )
            return
        self._thread = threading.Thread(
            target=self._message_loop, daemon=True, name="PowerMonitor",
        )
        self._thread.start()

    def stop(self) -> None:
        """关闭消息循环线程。"""
        self._stop.set()
        if self._hwnd:
            try:
                import win32con
                import win32gui
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def _message_loop(self) -> None:
        import win32con
        import win32gui

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32gui.GetModuleHandle(None)
        wc.lpszClassName = "BLEAutoUnlockPowerMonitor"
        wc.lpfnWndProc = self._wnd_proc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            logger.warning("注册电源监听窗口类失败", exc_info=True)
            return
        try:
            self._hwnd = win32gui.CreateWindow(
                wc.lpszClassName, "BLEAutoUnlock", 0, 0, 0, 0, 0, 0,
                0, wc.hInstance, None,
            )
        except Exception:
            logger.warning("创建电源监听窗口失败", exc_info=True)
            return
        win32gui.PumpMessages()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        import win32con
        import win32gui

        if msg == self.WM_POWERBROADCAST:
            if wparam in (self.PBT_APMRESUMEAUTOMATIC, self.PBT_APMRESUMECRITICAL):
                logger.info("收到电源恢复消息 (0x%08X)，触发唤醒回调", wparam)
                try:
                    self._on_resume()
                except Exception:
                    logger.exception("电源唤醒回调执行失败")
        elif msg in (win32con.WM_CLOSE, win32con.WM_DESTROY):
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


# ================================================================ 状态机


class AutoUnlockController:
    """根据 RSSI 序列驱动锁屏/解锁状态机。"""

    def __init__(self, config: ConfigManager, actions: Actions):
        self.config = config
        self.actions = actions
        self.unlock_rssi = int(config.get("unlock_rssi", -60))
        self.lock_rssi = int(config.get("lock_rssi", -75))
        self.miss_count_before_lock = int(config.get("miss_count_before_lock", 5))
        self.lock_min_elapsed = float(config.get("lock_min_elapsed", 10))
        self.unlock_hold_seconds = float(config.get("unlock_hold_seconds", 3))

        # 初始状态通过当前输入桌面判断；无法判断时视为未知（None）
        self.locked: Optional[bool] = is_session_locked()
        if self.locked is None:
            logger.warning("无法判断当前会话锁定状态，初始状态置为未知")
        else:
            logger.info("初始会话状态: %s", "已锁定" if self.locked else "未锁定")

        self._good_since: Optional[float] = None   # 首次进入解锁范围的时间
        self._miss_count = 0                       # 连续离开/消失次数
        self._miss_since: Optional[float] = None   # 首次离开/消失的时间
        self._last_rssi_log = 0.0                  # 上次周期日志时间

    def on_scan_result(self, rssi: Optional[int]) -> None:
        """处理一次扫描结果，按需触发解锁/锁定。"""
        now = time.monotonic()

        if rssi is not None and rssi >= self.unlock_rssi:
            # 目标靠近：重置离开计数，累计靠近时长
            self._miss_count = 0
            self._miss_since = None
            if self._good_since is None:
                self._good_since = now
                logger.info("目标设备进入解锁范围（RSSI=%s）", rssi)
            if self.locked is not False and (
                now - self._good_since
            ) >= self.unlock_hold_seconds:
                self._unlock()
        elif rssi is None or rssi < self.lock_rssi:
            # 目标离开或消失：重置靠近计时，累计离开次数
            self._good_since = None
            if self._miss_since is None:
                self._miss_since = now
            self._miss_count += 1
            if self.locked is not True and (
                self._miss_count >= self.miss_count_before_lock
                and (now - self._miss_since) >= self.lock_min_elapsed
            ):
                self._lock()
        else:
            # 迟滞区间（lock_rssi <= RSSI < unlock_rssi）：保持当前状态
            self._good_since = None
            self._miss_count = 0
            self._miss_since = None

        # 每 10 秒记录一次当前目标设备 RSSI
        if now - self._last_rssi_log >= 10.0:
            self._last_rssi_log = now
            logger.info(
                "当前目标设备 RSSI: %s（状态: %s）",
                "未发现" if rssi is None else str(rssi),
                self._state_text(),
            )

    def _state_text(self) -> str:
        if self.locked is None:
            return "未知"
        return "已锁定" if self.locked else "已解锁"

    def _unlock(self) -> None:
        """执行解锁动作。"""
        if self.locked is False:
            return
        logger.info("目标设备持续靠近 %.1f 秒，触发解锁", self.unlock_hold_seconds)
        if self.actions.unlock():
            self.locked = False
        else:
            logger.error("解锁失败，保持当前状态")
        self._good_since = None

    def _lock(self) -> None:
        """执行锁定动作。"""
        if self.locked is True:
            return
        elapsed = (time.monotonic() - self._miss_since) if self._miss_since else 0.0
        logger.info(
            "目标设备离开/消失（连续 %d 次扫描、累计 %.1f 秒），触发锁定",
            self._miss_count, elapsed,
        )
        if self.actions.lock():
            self.locked = True
        else:
            logger.error("锁定失败，保持当前状态")
        self._miss_count = 0
        self._miss_since = None


# ================================================================ 主循环


async def _async_main(config: ConfigManager, stop_event) -> None:
    """构建组件并运行扫描循环（供 --run 与服务模式共用）。"""
    try:
        resolver = create_resolver(config)
    except ResolverError as exc:
        logger.error("设备识别器初始化失败: %s", exc)
        raise
    logger.info("设备识别器: %s", resolver.description)

    # 延迟导入：让 --selftest / --set-password 在缺少 bleak 时仍可运行
    from scanner import AdapterResetter, Scanner

    resetter = AdapterResetter(
        retries=int(config.get("adapter_reset_retries", 3)),
        delay=float(config.get("adapter_reset_delay", 5)),
    )
    scanner = Scanner(
        resolver,
        scan_interval=float(config.get("scan_interval", 2)),
        scan_duration=float(config.get("scan_duration", 2)),
        resetter=resetter,
    )
    actions = Actions(config)
    controller = AutoUnlockController(config, actions)

    power_monitor = PowerMonitor(scanner.notify_wake)
    power_monitor.start()
    try:
        await scanner.run(controller.on_scan_result, stop_event)
    finally:
        power_monitor.stop()


def _run_async(config: ConfigManager, stop_event) -> None:
    """在独立事件循环中运行 _async_main，并处理 Ctrl+C。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_main(config, stop_event))
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在退出...")
        stop_event.set()
    finally:
        loop.close()


def run_foreground(config_path: Optional[str] = None,
                   stop_event: Optional[threading.Event] = None,
                   service_mode: bool = False) -> None:
    """加载配置并运行主循环（--run 与服务模式共用）。"""
    try:
        config = ConfigManager(config_path).load()
    except ConfigError as exc:
        logger.error("配置加载失败: %s", exc)
        return
    setup_logging(
        level=config.get("log_level", "INFO"),
        log_dir=config.get("log_dir"),
        console=not service_mode,
    )

    instance = SingleInstance()
    if instance.already_running:
        logger.error("另一个 BLEAutoUnlock 实例正在运行，本实例退出")
        return
    try:
        if stop_event is None:
            stop_event = threading.Event()
        if is_admin():
            logger.info("当前进程以管理员权限运行")
        else:
            logger.warning(
                "当前进程非管理员权限；键盘模拟输入密码解锁可能被系统拦截"
            )
        _run_async(config, stop_event)
    except Exception:
        logger.exception("主循环异常退出")
    finally:
        instance.close()


# ================================================================ 自检


def _load_tests_module():
    """通过文件路径加载 tests/test_resolver.py（避免依赖包结构）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "tests", "test_resolver.py")
    spec = importlib.util.spec_from_file_location("ble_autounlock_selftests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_selftest(config_path: Optional[str] = None) -> int:
    """运行自检：IRK 算法向量、配置、依赖、权限。"""
    print("=" * 52)
    print("BLEAutoUnlock 自检")
    print("=" * 52)
    failures = 0

    # 1. 算法向量
    print("[1/4] IRK/AES 算法向量...")
    try:
        tests = _load_tests_module()
        if tests.run_vectors():
            print("      通过：NIST FIPS-197 C.1 + 泰凌微 + EnOcean 向量")
        else:
            print("      失败：算法向量校验未通过")
            failures += 1
    except Exception as exc:
        print(f"      失败：无法运行向量测试: {exc}")
        failures += 1

    # 2. 配置检查
    print("[2/4] 配置检查...")
    try:
        config = ConfigManager(config_path).load()
        print(f"      配置文件: {config.path}")
        if config.get("device_type") == "ios":
            irk = str(config.get("irk_key") or "")
            if irk:
                print(f"      irk_key 已配置（{len(irk)} 个字符）")
                resolver = create_resolver(config)
                print(f"      识别器: {resolver.description}")
            else:
                print("      警告：irk_key 为空，扫描将无法识别 iPhone（请填入 64 位 IRK）")
                failures += 1
        else:
            print(f"      device_type = {config.get('device_type')}")
        if config.get("windows_password"):
            print("      windows_password 已配置（DPAPI 加密存储）")
        else:
            print("      警告：windows_password 为空，键盘解锁方案不可用"
                  "（可用 python main.py --set-password 配置）")
    except (ConfigError, ResolverError) as exc:
        print(f"      失败：{exc}")
        failures += 1

    # 3. 依赖检查
    print("[3/4] 依赖检查...")
    for dep in ("bleak", "win32crypt", "keyboard"):
        try:
            __import__(dep)
            print(f"      {dep}: 已安装")
        except ImportError:
            print(f"      {dep}: 未安装（请执行 pip install -r requirements.txt）")
            failures += 1

    # 4. 权限检查
    print("[4/4] 权限检查...")
    print(f"      管理员权限: {'是' if is_admin() else '否（键盘解锁可能被拦截）'}")
    locked = is_session_locked()
    print(f"      当前会话锁定状态: {locked}")

    print("=" * 52)
    if failures:
        print(f"自检完成：{failures} 项未通过")
        return 1
    print("自检全部通过")
    return 0


# ================================================================ 参数入口


def parse_args(argv=None):
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="BLEAutoUnlock：睡眠唤醒后根据 iPhone 蓝牙 RSSI 解锁/锁定桌面",
    )
    parser.add_argument("--run", action="store_true",
                        help="前台运行（调试模式，带控制台日志）")
    parser.add_argument("--install", action="store_true",
                        help="安装为 Windows 服务（需管理员权限）")
    parser.add_argument("--uninstall", action="store_true",
                        help="卸载 Windows 服务（需管理员权限）")
    parser.add_argument("--set-password", action="store_true",
                        help="交互式加密保存 Windows 登录密码")
    parser.add_argument("--selftest", action="store_true", help="运行自检")
    parser.add_argument("--gui", action="store_true",
                        help="启动图形界面（配置 IRK/蓝牙阈值/判定时间）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 main.py 同目录 config.json）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """程序入口。"""
    args = parse_args(argv)

    # 服务安装/卸载不依赖配置文件
    if args.install:
        if not is_admin():
            print("错误：安装服务需要管理员权限，请以管理员身份运行")
            return 1
        from service import SERVICE_NAME, install
        install()
        print(f"服务安装完成：{SERVICE_NAME}，可使用 sc start {SERVICE_NAME} 启动")
        return 0

    if args.uninstall:
        if not is_admin():
            print("错误：卸载服务需要管理员权限，请以管理员身份运行")
            return 1
        from service import uninstall
        uninstall()
        print("服务卸载完成")
        return 0

    # 其余子命令需要加载配置
    try:
        config = ConfigManager(args.config).load()
    except ConfigError as exc:
        print(f"错误：配置加载失败: {exc}")
        return 1
    setup_logging(
        level=config.get("log_level", "INFO"),
        log_dir=config.get("log_dir"),
        console=True,
    )

    if args.set_password:
        import getpass
        password = getpass.getpass(
            "请输入 Windows 登录密码（仅用于本地解锁，DPAPI 加密存储）: ",
        )
        if not password:
            print("错误：密码不能为空")
            return 1
        confirm = getpass.getpass("请再次输入确认: ")
        if password != confirm:
            print("错误：两次输入不一致")
            return 1
        try:
            config.set_password_encrypted(password)
            config.save()
        except ConfigError as exc:
            print(f"错误：密码加密保存失败: {exc}")
            return 1
        print("密码已通过 DPAPI 加密保存到 config.json")
        return 0

    if args.selftest:
        return run_selftest(args.config)

    if args.gui:
        from gui import main as gui_main
        return gui_main(args.config)

    # 默认或 --run：前台运行
    run_foreground(config_path=args.config, stop_event=None, service_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

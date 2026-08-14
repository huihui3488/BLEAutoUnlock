"""BLE 扫描模块：使用 bleak 持续扫描，识别目标设备并返回 RSSI。

- 单次扫描持续 scan_duration 秒（默认 2 秒），两次扫描之间间隔 scan_interval 秒
- 每个可见设备都交给 resolver.matches() 判断是否为目标设备
- 蓝牙适配器异常（典型场景：睡眠唤醒后适配器"设备不可用"）时，
  自动尝试重置适配器（devcon 优先，其次 PowerShell PnP 禁用/启用，
  最后 btpair 兼容入口），延迟 adapter_reset_delay 秒重试，
  最多 adapter_reset_retries 次
- 睡眠唤醒后自动恢复扫描：主循环异常重连 + notify_wake() 主动重置扫描状态
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from typing import Callable, Optional

from bleak import BleakScanner

logger = logging.getLogger(__name__)


async def sleep_interruptible(seconds: float, stop_event) -> None:
    """分段睡眠；stop_event 置位时立即返回，便于及时响应服务停止。"""
    deadline = time.monotonic() + seconds
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.5))


class AdapterResetter:
    """蓝牙适配器重置器。

    依次尝试三种策略：
      1) devcon restart / disable+enable（需要 WDK 的 devcon.exe，
         可通过 DEVCON_PATH 环境变量或配置文件指定路径）
      2) PowerShell PnP 禁用/启用处于异常状态的蓝牙设备（需管理员权限）
      3) btpair 兼容入口（btpair 实为配对管理工具，通常无法重置适配器，
         保留该分支仅为了兼容需求描述，多数情况下不会成功）
    """

    def __init__(self, retries: int = 3, delay: float = 5.0,
                 devcon_path: Optional[str] = None):
        self.retries = max(1, int(retries))
        self.delay = max(0.0, float(delay))
        self.devcon = (devcon_path
                       or os.environ.get("DEVCON_PATH")
                       or shutil.which("devcon"))

    def reset(self) -> bool:
        """尝试重置蓝牙适配器；任一种策略成功即返回 True。"""
        strategies = []
        if self.devcon:
            strategies.append(("devcon", self._reset_with_devcon))
        strategies.append(("PowerShell PnP", self._reset_with_powershell))
        strategies.append(("btpair", self._reset_with_btpair))

        for name, func in strategies:
            try:
                if func():
                    logger.info("蓝牙适配器已通过 %s 重置成功", name)
                    return True
            except Exception as exc:
                logger.warning("通过 %s 重置适配器失败: %s", name, exc)
        return False

    def _reset_with_devcon(self) -> bool:
        """用 devcon 重启蓝牙设备；不支持 restart 时退化为 disable+enable。"""
        logger.info("尝试用 devcon 重置蓝牙适配器: %s", self.devcon)
        result = subprocess.run(
            [self.devcon, "restart", r"BTH\*"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return True
        # 部分 devcon 版本不支持 restart，改用 disable + enable
        disable = subprocess.run(
            [self.devcon, "disable", r"BTH\*"],
            capture_output=True, text=True, timeout=60,
        )
        time.sleep(2)
        enable = subprocess.run(
            [self.devcon, "enable", r"BTH\*"],
            capture_output=True, text=True, timeout=60,
        )
        if disable.returncode != 0:
            logger.warning("devcon disable 返回码 %s: %s",
                           disable.returncode, disable.stdout.strip())
        return enable.returncode == 0

    def _reset_with_powershell(self) -> bool:
        """用 PowerShell PnP 命令禁用/启用状态异常的蓝牙设备（需管理员）。"""
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "$devs = Get-PnpDevice -Class Bluetooth | "
            "Where-Object { $_.Status -in @('Error','Unknown') }; "
            "if (-not $devs) { exit 0 }; "
            "$devs | Disable-PnpDevice -Confirm:$false; "
            "Start-Sleep -Seconds 2; "
            "$devs | Enable-PnpDevice -Confirm:$false"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            logger.warning("PowerShell 重置适配器返回码 %s: %s",
                           result.returncode, result.stderr.strip() or result.stdout.strip())
        return result.returncode == 0

    def _reset_with_btpair(self) -> bool:
        """btpair 兼容入口（通常无法真正重置适配器，仅作为最后手段）。"""
        btpair = shutil.which("btpair")
        if not btpair:
            return False
        result = subprocess.run(
            [btpair, "-u"], capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0


class Scanner:
    """BLE 扫描器：持续扫描并回调目标设备的 RSSI。"""

    def __init__(self, resolver, scan_interval: float = 2.0,
                 scan_duration: float = 2.0,
                 resetter: Optional[AdapterResetter] = None):
        self.resolver = resolver
        self.scan_interval = max(0.5, float(scan_interval))
        self.scan_duration = max(0.5, float(scan_duration))
        self.resetter = resetter or AdapterResetter()
        self._wake_flag = False
        self._last_scan_start = 0.0

    def notify_wake(self) -> None:
        """电源监视器在系统唤醒后调用：标记需要重置扫描状态。"""
        self._wake_flag = True
        logger.info("收到系统唤醒通知，将在下一次扫描时重置扫描状态")

    async def _scan_once(self, stop_event) -> Optional[int]:
        """扫描 scan_duration 秒，返回目标设备最新 RSSI；未发现目标返回 None。"""
        target_rssi: Optional[int] = None

        def on_detect(device, advertisement_data):
            nonlocal target_rssi
            try:
                if self.resolver.matches(device.address, advertisement_data.rssi):
                    target_rssi = advertisement_data.rssi
                    logger.debug("发现目标设备 %s, RSSI=%s",
                                 device.address, target_rssi)
            except Exception:
                logger.exception("识别目标设备时发生异常: %s", device.address)

        async with BleakScanner(
            detection_callback=on_detect, scanning_mode="passive",
        ) as _scanner:
            # 每次扫描都新建 watcher，确保睡眠唤醒后使用全新扫描状态
            await sleep_interruptible(self.scan_duration, stop_event)
        return target_rssi

    async def run(self, on_result: Callable[[Optional[int]], None],
                  stop_event) -> None:
        """持续扫描主循环。

        :param on_result: 每次扫描完成后的回调，参数为目标设备 RSSI
            （None 表示本次扫描未发现目标设备）
        :param stop_event: threading.Event，置位后退出循环
        """
        consecutive_failures = 0
        while stop_event is None or not stop_event.is_set():
            if self._wake_flag:
                self._wake_flag = False
                consecutive_failures = 0
                logger.info("扫描状态已重置（系统唤醒后）")

            try:
                self._last_scan_start = time.monotonic()
                rssi = await self._scan_once(stop_event)
                consecutive_failures = 0
                try:
                    on_result(rssi)
                except Exception:
                    logger.exception("处理扫描结果回调时发生异常")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 典型场景：睡眠唤醒后蓝牙适配器报"设备不可用"
                consecutive_failures += 1
                logger.warning("蓝牙扫描异常（连续第 %d 次）: %s",
                               consecutive_failures, exc)
                if consecutive_failures <= self.resetter.retries:
                    logger.info("尝试重置蓝牙适配器，%s 秒后重试",
                                self.resetter.delay)
                    if not self.resetter.reset():
                        logger.error("蓝牙适配器重置失败，等待后继续重试")
                elif consecutive_failures % 10 == 0:
                    logger.error(
                        "蓝牙适配器持续异常（已重置 %d 次），继续后台重试",
                        self.resetter.retries,
                    )
                await sleep_interruptible(self.resetter.delay, stop_event)
                continue

            # 两次扫描之间的间隔
            await sleep_interruptible(self.scan_interval, stop_event)

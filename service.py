"""Windows 服务封装（pywin32 / win32serviceutil）。

安装方式：python main.py --install（需管理员权限）。
服务以 <python.exe> service.py 方式宿主（见 _exe_name_/_exe_args_），
由 SCM 拉起时无参数运行，进入 servicemanager 调度循环，无控制台窗口。

重要限制：
    Windows 服务默认运行在 Session 0，无法访问用户的交互桌面，
    建议使用用户登录自启方式（main.py --run + 启动项/任务计划）运行。
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

SERVICE_NAME = "BLEAutoUnlock"
SERVICE_DISPLAY_NAME = "BLEAutoUnlock Service"
SERVICE_DESCRIPTION = "睡眠唤醒后根据 iPhone 蓝牙 RSSI 自动锁屏工作站"

try:
    import servicemanager
    import win32service
    import win32serviceutil
except ImportError:  # 缺少 pywin32 时仍允许导入（便于自检/py_compile）
    servicemanager = None
    win32service = None
    win32serviceutil = None


if win32serviceutil is not None:
    class BLEAutoUnlockService(win32serviceutil.ServiceFramework):
        """pywin32 服务框架实现。"""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION
        # 让 SCM 直接运行 <python.exe> <service.py>，服务进程自行进入调度循环
        _exe_name_ = sys.executable
        _exe_args_ = f'"{os.path.abspath(__file__)}"'

        def __init__(self, args):
            super().__init__(args)
            self._stop_event = threading.Event()

        def SvcStop(self) -> None:
            """服务停止：置位停止事件，让主循环尽快退出。"""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop_event.set()
            logger.info("收到服务停止请求")

        def SvcDoRun(self) -> None:
            """服务运行入口。"""
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            logger.info("服务开始运行: %s", SERVICE_NAME)
            self.main()

        def main(self) -> None:
            """运行主逻辑（延迟导入避免与 main.py 循环依赖）。"""
            from main import run_foreground
            run_foreground(stop_event=self._stop_event, service_mode=True)
else:
    BLEAutoUnlockService = None  # 缺少 pywin32 时的占位，便于导入/自检


def main(argv=None) -> None:
    """服务模块入口。

    - 无参数：由 SCM 拉起，进入服务调度循环（服务宿主方式）
    - 带参数：交给 win32serviceutil 处理（--install / --uninstall / start 等）
    """
    argv = list(sys.argv if argv is None else argv)
    if len(argv) == 1:
        if servicemanager is None:
            sys.exit("缺少 pywin32，无法以服务模式运行")
        servicemanager.Initialize()
        servicemanager.PrepareServiceHost(BLEAutoUnlockService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        if win32serviceutil is None:
            sys.exit("缺少 pywin32，无法执行服务管理命令")
        win32serviceutil.HandleCommandLine(BLEAutoUnlockService, argv)


def install() -> None:
    """安装服务并设为开机自启（需管理员权限）。

    显式调用 win32serviceutil.InstallService：
    - ImagePath 为 <python.exe> <service.py>（自宿主模式，无控制台窗口）
    - startType 固定为 SERVICE_AUTO_START，满足"开机自启"需求
    """
    if win32serviceutil is None:
        raise RuntimeError("缺少 pywin32，无法安装服务")
    win32serviceutil.InstallService(
        "service.BLEAutoUnlockService",  # 写入注册表的 PythonClass（自宿主模式下不参与启动）
        SERVICE_NAME,
        SERVICE_DISPLAY_NAME,
        startType=win32service.SERVICE_AUTO_START,
        description=SERVICE_DESCRIPTION,
        exeName=sys.executable,
        exeArgs=f'"{os.path.abspath(__file__)}"',
    )
    logger.info("服务安装完成（已设为自动启动）: %s", SERVICE_NAME)


def uninstall() -> None:
    """卸载服务（需管理员权限）。"""
    if win32serviceutil is None:
        raise RuntimeError("缺少 pywin32，无法卸载服务")
    win32serviceutil.RemoveService(SERVICE_NAME)
    logger.info("服务卸载完成: %s", SERVICE_NAME)


if __name__ == "__main__":
    main()

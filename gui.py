"""图形界面入口（tkinter 控制面板）。

功能：
- 配置并保存：设备类型 / 命名 IRK 设备列表（可同时管理 iPhone、iPad 等）/
  Android MAC / Windows 登录密码（DPAPI 加密）
- 蓝牙阈值：近场 RSSI、锁屏 RSSI
- 判定时间：扫描间隔、单次扫描时长、靠近持续时长、离开累计时长、连续未检测次数
- 高级选项：IRK 解析方法、prand 位置
- 二级菜单（帮助 → RSSI 距离对照表）：查看官方分级与参考距离换算
- 启停监听：后台线程运行 BLE 扫描状态机，界面实时显示 RSSI 与会话状态
- 日志面板：实时显示程序日志

打包：PyInstaller 单文件无控制台 exe，见 build.bat / BLEAutoUnlock.spec。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from actions import Actions, is_admin, is_session_locked
from config_manager import ConfigManager, ConfigError
from logger import LOG_DATE_FORMAT, LOG_FORMAT, setup_logging
from main import AutoUnlockController, PowerMonitor, SingleInstance
from resolver import ResolverError, create_resolver, normalize_address

logger = logging.getLogger(__name__)


def _write_crash_log(message: str) -> None:
    """把启动/运行错误追加写入 %APPDATA%\\BLEAutoUnlock\\gui_error.log。"""
    import datetime
    import os

    try:
        log_dir = os.environ.get("APPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(log_dir, "BLEAutoUnlock")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "gui_error.log")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def _show_error_dialog(title: str, message: str) -> None:
    """Tk 不可用时用系统 MessageBox 弹出错误，避免无控制台看不到报错。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _install_excepthook() -> None:
    """把未捕获异常写入日志（窗口回调里的异常不会打印到控制台）。"""
    import traceback

    def _hook(exc_type, exc_value, exc_tb):
        _write_crash_log(
            "未捕获异常:\n"
            + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    sys.excepthook = _hook


_install_excepthook()


class GuiLogHandler(logging.Handler):
    """把日志记录放入队列，由 GUI 主线程轮询后显示。"""

    def __init__(self, msg_queue: "queue.Queue"):
        super().__init__()
        self._queue = msg_queue
        self.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put(("log", self.format(record)))
        except Exception:
            pass


class BLEAutoUnlockApp:
    """tkinter 控制面板主程序。"""

    # RSSI 距离对照表：基于 n=2 对数路径损耗模型并综合公开实测资料的参考值。
    # 列顺序: (距离, 参考 RSSI dBm, 说明)
    RSSI_DISTANCE_TABLE = (
        ("0.1 m", "-31", "贴身 / 放在桌上"),
        ("0.5 m", "-45", "Immediate（Apple iBeacon < 0.5 m）"),
        ("1 m", "-51", "很近"),
        ("2 m", "-57", "Near（Apple iBeacon 0.5~3 m）"),
        ("3 m", "-60", "Near 上限（约 3 m）"),
        ("5 m", "-65", "中等距离"),
        ("8 m", "-72", "较远"),
        ("10 m", "-75", "Far 参考，约等于默认锁定阈值"),
        ("15 m", "-82", "更远"),
        ("20 m", "-88", "远 / 有遮挡"),
        ("30 m", "-95", "极限 / 强遮挡"),
    )

    def __init__(self, root: tk.Tk, config_path: Optional[str] = None):
        self.root = root
        self.config_path = config_path
        self.config = ConfigManager(config_path).load()
        # 命名 IRK 设备列表，元素: {"name": str, "key": str}；与界面 Treeview 同步
        self.irk_items = []
        self.msg_queue: "queue.Queue" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self._controller: Optional[AutoUnlockController] = None
        self._last_rssi: Optional[int] = None
        self._session_state: Optional[bool] = is_session_locked()
        self._running = False
        self._poll_job: Optional[str] = None

        self._setup_logging()
        self._build_ui()
        self._load_values()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = self.root.after(200, self._poll_queue)
        self._append_log("图形界面已启动")
        self._append_log(f"配置文件: {self.config.path}")
        if is_admin():
            self._append_log("当前进程以管理员权限运行")
        else:
            self._append_log("提示：当前非管理员权限，部分功能可能受限")

    # ------------------------------------------------------------- 日志与状态

    def _setup_logging(self) -> None:
        """初始化文件日志并把日志转发到 GUI 队列。"""
        setup_logging(
            level=self.config.get("log_level", "INFO"),
            log_dir=self.config.get("log_dir"),
            console=False,
        )
        gui_handler = GuiLogHandler(self.msg_queue)
        gui_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(gui_handler)

    def _append_log(self, text: str) -> None:
        """往日志文本框追加一行（保留最近 2000 行）。"""
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, text + "\n")
            # 裁剪过长的日志
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 2000:
                self.log_text.delete("1.0", f"{line_count - 2000}.0")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass  # 窗口已销毁

    def _update_status(self) -> None:
        state_text = {True: "已锁定", False: "未锁定", None: "未知"}.get(
            self._session_state, "未知",
        )
        rssi_text = "--" if self._last_rssi is None else f"{self._last_rssi} dBm"
        self.var_status.set(
            f"监听: {'运行中' if self._running else '未运行'}    "
            f"当前 RSSI: {rssi_text}    会话: {state_text}",
        )

    def _poll_queue(self) -> None:
        """轮询后台线程消息队列并刷新界面。"""
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "rssi":
                    self._last_rssi = payload
                    self._update_status()
                elif kind == "state":
                    self._session_state = payload
                    self._update_status()
                elif kind == "stopped":
                    self._set_running(False)
                    self._append_log("监听已停止")
        except queue.Empty:
            pass
        if self._poll_job is not None:
            try:
                self._poll_job = self.root.after(200, self._poll_queue)
            except tk.TclError:
                self._poll_job = None

    # ------------------------------------------------------------- 界面布局

    def _build_ui(self) -> None:
        self.root.title("BLEAutoUnlock 控制面板")
        self.root.geometry("760x800")
        self.root.minsize(660, 680)

        # ---- 菜单栏（二级菜单）
        menubar = tk.Menu(self.root)
        menu_help = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="帮助(H)", menu=menu_help)
        menu_help.add_command(
            label="RSSI 距离对照表", command=self._open_rssi_reference,
        )
        self.root.config(menu=menubar)

        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill="both", expand=True)

        # ---- 设备与密钥
        frm_device = ttk.LabelFrame(main_frame, text="设备与密钥", padding=8)
        frm_device.pack(fill="x", pady=(0, 8))
        frm_device.columnconfigure(1, weight=1)

        self.var_device = tk.StringVar(value="ios")
        self.var_android_mac = tk.StringVar()
        self.var_irk = tk.StringVar()
        self.var_irk_name = tk.StringVar()
        self.var_password = tk.StringVar()

        ttk.Label(frm_device, text="设备类型:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(
            frm_device, textvariable=self.var_device, state="readonly",
            values=("ios", "android"), width=10,
        ).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(frm_device, text="Android MAC (预留):").grid(
            row=0, column=2, sticky="e", padx=(16, 6), pady=3,
        )
        ttk.Entry(frm_device, textvariable=self.var_android_mac, width=18).grid(
            row=0, column=3, sticky="w", pady=3,
        )

        # ---- 命名 IRK 设备列表（可添加多台设备）
        ttk.Label(frm_device, text="IRK 设备列表:").grid(
            row=1, column=0, sticky="ne", padx=(0, 6), pady=3,
        )
        tree_frame = ttk.Frame(frm_device)
        tree_frame.grid(row=1, column=1, columnspan=3, sticky="we", pady=3)
        self.irk_tree = ttk.Treeview(
            tree_frame, columns=("name", "key"), show="headings", height=4,
        )
        self.irk_tree.heading("name", text="设备名称")
        self.irk_tree.heading("key", text="IRK (64位十六进制)")
        self.irk_tree.column("name", width=100, anchor="w")
        self.irk_tree.column("key", width=320, anchor="w")
        tree_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.irk_tree.yview,
        )
        self.irk_tree.configure(yscrollcommand=tree_scroll.set)
        self.irk_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # 添加 / 删除设备的一行输入控件
        irk_entry_frame = ttk.Frame(frm_device)
        irk_entry_frame.grid(row=2, column=1, columnspan=3, sticky="we", pady=3)
        ttk.Label(irk_entry_frame, text="名称:").pack(side="left")
        ttk.Entry(
            irk_entry_frame, textvariable=self.var_irk_name, width=12,
        ).pack(side="left", padx=(2, 10))
        ttk.Label(irk_entry_frame, text="IRK:").pack(side="left")
        ttk.Entry(
            irk_entry_frame, textvariable=self.var_irk, width=30,
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            irk_entry_frame, text="添加", command=self._on_add_irk,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            irk_entry_frame, text="删除选中", command=self._on_delete_irk,
        ).pack(side="left", padx=(6, 0))

        ttk.Label(
            frm_device, foreground="#666666",
            text="IRK 为 64 位十六进制（32 字符）；可添加多台命名设备，任一命中即视为靠近",
        ).grid(row=3, column=1, columnspan=3, sticky="w", padx=0, pady=(0, 2))

        ttk.Label(frm_device, text="Windows 密码:").grid(
            row=4, column=0, sticky="e", padx=(0, 6), pady=3,
        )
        ttk.Entry(frm_device, textvariable=self.var_password, show="*", width=28).grid(
            row=4, column=1, sticky="w", pady=3,
        )
        ttk.Button(frm_device, text="DPAPI 加密保存密码", command=self._on_save_password).grid(
            row=4, column=2, columnspan=2, sticky="w", padx=(16, 0), pady=3,
        )

        # ---- 蓝牙阈值
        frm_bt = ttk.LabelFrame(main_frame, text="蓝牙阈值 (dBm)", padding=8)
        frm_bt.pack(fill="x", pady=(0, 8))

        self.var_unlock_rssi = tk.StringVar(value="-60")
        self.var_lock_rssi = tk.StringVar(value="-75")
        self._add_spin(frm_bt, 0, 0, "近场阈值 (≥):", self.var_unlock_rssi, -100, 0, 1)
        self._add_spin(frm_bt, 0, 1, "锁屏 RSSI (<):", self.var_lock_rssi, -100, 0, 1)
        ttk.Label(
            frm_bt, foreground="#666666",
            text="设备信号高于近场阈值时视为在附近；低于锁屏值或设备消失且连续未检测才考虑锁屏",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 0))

        # ---- 判定时间
        frm_time = ttk.LabelFrame(main_frame, text="判定时间 (秒)", padding=8)
        frm_time.pack(fill="x", pady=(0, 8))

        self.var_scan_interval = tk.StringVar(value="2")
        self.var_scan_duration = tk.StringVar(value="2")
        self.var_hold = tk.StringVar(value="3")
        self.var_elapsed = tk.StringVar(value="10")
        self.var_misses = tk.StringVar(value="5")

        self._add_spin(frm_time, 0, 0, "扫描间隔:", self.var_scan_interval, 0.5, 60, 0.5)
        self._add_spin(frm_time, 0, 1, "单次扫描时长:", self.var_scan_duration, 0.5, 60, 0.5)
        self._add_spin(frm_time, 1, 0, "靠近持续时长:", self.var_hold, 1, 120, 1)
        self._add_spin(frm_time, 1, 1, "离开累计时长:", self.var_elapsed, 1, 600, 1)
        self._add_spin(frm_time, 2, 0, "连续未检测次数:", self.var_misses, 1, 30, 1)

        # ---- 高级选项
        frm_adv = ttk.LabelFrame(main_frame, text="高级选项", padding=8)
        frm_adv.pack(fill="x", pady=(0, 8))

        self.var_method = tk.StringVar(value="ble_standard")
        self.var_prand = tk.StringVar(value="")
        ttk.Label(frm_adv, text="IRK 解析方法:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(
            frm_adv, textvariable=self.var_method, state="readonly",
            values=("ble_standard", "legacy_hmac"), width=16,
        ).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(frm_adv, text="prand 位置 (留空=自动):").grid(
            row=0, column=2, sticky="e", padx=(16, 6), pady=3,
        )
        ttk.Combobox(
            frm_adv, textvariable=self.var_prand, state="readonly",
            values=("", "head", "tail"), width=8,
        ).grid(row=0, column=3, sticky="w", pady=3)

        # ---- 操作按钮与状态
        frm_ctrl = ttk.Frame(main_frame)
        frm_ctrl.pack(fill="x", pady=(0, 8))
        self.btn_save = ttk.Button(frm_ctrl, text="保存配置", command=self._on_save)
        self.btn_save.pack(side="left", padx=(0, 8))
        self.btn_start = ttk.Button(frm_ctrl, text="开始监听", command=self._on_start)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_stop = ttk.Button(frm_ctrl, text="停止监听", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left")

        self.var_status = tk.StringVar()
        ttk.Label(frm_ctrl, textvariable=self.var_status, foreground="#0066CC").pack(
            side="right",
        )
        self._update_status()

        # ---- 日志面板
        frm_log = ttk.LabelFrame(main_frame, text="运行日志", padding=4)
        frm_log.pack(fill="both", expand=True)
        self.log_text = tk.Text(frm_log, height=12, state="disabled", wrap="word",
                                font=("Microsoft YaHei UI", 9))
        scrollbar = ttk.Scrollbar(frm_log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _add_spin(self, parent, row: int, col: int, label: str, var,
                  from_: float, to: float, increment: float) -> None:
        """在网格中放置一个带标签的数字输入框。"""
        ttk.Label(parent, text=label).grid(
            row=row, column=col * 2, sticky="e", padx=(8, 4), pady=3,
        )
        ttk.Spinbox(
            parent, from_=from_, to=to, increment=increment,
            textvariable=var, width=8,
        ).grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=3)

    def _load_values(self) -> None:
        """把配置文件的值填充到界面控件。"""
        self.var_device.set(str(self.config.get("device_type", "ios")))
        self.var_android_mac.set(str(self.config.get("android_mac", "") or ""))

        # 命名 IRK 列表：优先读取 irk_keys；为空时兼容旧版单 irk_key 配置
        self.irk_items = []
        for index, item in enumerate(self.config.get("irk_keys") or []):
            if isinstance(item, dict):
                name = str(item.get("name", "") or "").strip() or f"设备{index + 1}"
                key = str(item.get("key", "") or "").strip()
            else:
                name, key = f"设备{index + 1}", str(item or "").strip()
            self.irk_items.append({"name": name, "key": key})
        if not self.irk_items and self.config.get("irk_key"):
            self.irk_items.append({
                "name": "默认", "key": str(self.config.get("irk_key", "")).strip(),
            })
        self._refresh_irk_tree()

        self.var_unlock_rssi.set(str(self.config.get("unlock_rssi", -60)))
        self.var_lock_rssi.set(str(self.config.get("lock_rssi", -75)))
        self.var_scan_interval.set(str(self.config.get("scan_interval", 2)))
        self.var_scan_duration.set(str(self.config.get("scan_duration", 2)))
        self.var_hold.set(str(self.config.get("unlock_hold_seconds", 3)))
        self.var_elapsed.set(str(self.config.get("lock_min_elapsed", 10)))
        self.var_misses.set(str(self.config.get("miss_count_before_lock", 5)))
        self.var_method.set(str(self.config.get("irk_resolve_method", "ble_standard")))
        self.var_prand.set(str(self.config.get("irk_prand_position", "") or ""))

    def _refresh_irk_tree(self) -> None:
        """把 self.irk_items 同步显示到 Treeview 列表。"""
        for item in self.irk_tree.get_children():
            self.irk_tree.delete(item)
        for index, entry in enumerate(self.irk_items):
            self.irk_tree.insert(
                "", tk.END, iid=str(index),
                values=(entry["name"], entry["key"]),
            )

    def _on_add_irk(self) -> None:
        """把界面上的名称 + IRK 校验后加入命名设备列表。"""
        name = self.var_irk_name.get().strip()
        key = self.var_irk.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写要添加的 IRK")
            return
        compact = key.lower().replace("0x", "")
        if len(compact) != 32:
            messagebox.showerror("格式错误", "IRK 必须是 64 位十六进制字符串（32 个字符）")
            return
        try:
            bytes.fromhex(compact)
        except ValueError:
            messagebox.showerror("格式错误", "IRK 包含非法十六进制字符")
            return
        if not name:
            name = f"设备{len(self.irk_items) + 1}"
        self.irk_items.append({"name": name, "key": compact})
        self.var_irk_name.set("")
        self.var_irk.set("")
        self._refresh_irk_tree()
        self._append_log(f"已添加设备: {name}")

    def _on_delete_irk(self) -> None:
        """删除 Treeview 中选中的设备。"""
        selection = self.irk_tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先在列表中选中要删除的设备")
            return
        # 从大到小删除，避免前面删除导致后面索引错位
        for iid in sorted((int(item) for item in selection), reverse=True):
            self.irk_items.pop(iid)
        self._refresh_irk_tree()
        self._append_log("已删除选中的设备")

    # ------------------------------------------------------------- 配置读写

    def _collect_values(self, save: bool = False):
        """读取并校验界面输入；save=True 时写入配置文件。

        :return: (ok, 错误信息或空字符串)
        """
        try:
            device_type = self.var_device.get().strip().lower()
            if device_type not in ("ios", "android"):
                return False, "设备类型只能是 ios 或 android"

            # 收集并兜底校验命名 IRK 列表（添加时已校验，此处防止状态漂移）
            irk_keys = []
            for entry in self.irk_items:
                name = str(entry.get("name", "") or "").strip()
                key = str(entry.get("key", "") or "").strip()
                compact = key.lower().replace("0x", "")
                if not compact:
                    continue
                if len(compact) != 32:
                    return False, f"设备「{name}」的 IRK 必须是 64 位十六进制字符串（32 个字符）"
                try:
                    bytes.fromhex(compact)
                except ValueError:
                    return False, f"设备「{name}」的 IRK 包含非法十六进制字符"
                irk_keys.append({"name": name, "key": compact})

            if device_type == "ios" and not irk_keys:
                return False, "device_type=ios 时需要至少一个 IRK（请先添加设备）"
            if device_type == "android":
                mac = self.var_android_mac.get().strip()
                if not mac:
                    return False, "device_type=android 时需要填写 Android MAC"
                if normalize_address(mac) is None:
                    return False, "Android MAC 格式非法（应为 AA:BB:CC:DD:EE:FF）"

            unlock_rssi = int(float(self.var_unlock_rssi.get()))
            lock_rssi = int(float(self.var_lock_rssi.get()))
            if not (-100 <= unlock_rssi <= 0 and -100 <= lock_rssi <= 0):
                return False, "RSSI 取值范围应为 -100 ~ 0"

            scan_interval = float(self.var_scan_interval.get())
            scan_duration = float(self.var_scan_duration.get())
            hold = float(self.var_hold.get())
            elapsed = float(self.var_elapsed.get())
            misses = int(float(self.var_misses.get()))
            if min(scan_interval, scan_duration, hold, elapsed) <= 0 or misses < 1:
                return False, "时间参数必须为正数，连续未检测次数至少为 1"

            values = {
                "device_type": device_type,
                "irk_keys": irk_keys,
                "irk_key": irk_keys[0]["key"] if irk_keys else "",
                "android_mac": self.var_android_mac.get().strip(),
                "unlock_rssi": unlock_rssi,
                "lock_rssi": lock_rssi,
                "scan_interval": scan_interval,
                "scan_duration": scan_duration,
                "unlock_hold_seconds": hold,
                "lock_min_elapsed": elapsed,
                "miss_count_before_lock": misses,
                "irk_resolve_method": self.var_method.get(),
                "irk_prand_position": self.var_prand.get().strip(),
            }
            for key, value in values.items():
                self.config.set(key, value)
            if save:
                self.config.save()
            if unlock_rssi <= lock_rssi:
                self._append_log("提示：近场阈值未高于锁屏阈值，判定区间无效，可能频繁切换")
            return True, ""
        except (ValueError, TypeError):
            return False, "请检查输入：RSSI/时间为数字，未检测次数为整数"

    def _on_save(self) -> None:
        ok, msg = self._collect_values(save=True)
        if not ok:
            messagebox.showerror("保存失败", msg)
            return
        self._append_log("配置已保存")

    def _on_save_password(self) -> None:
        """用 DPAPI 加密保存 Windows 登录密码。"""
        password = self.var_password.get()
        if not password:
            messagebox.showwarning("提示", "请输入要加密保存的 Windows 登录密码")
            return
        try:
            self.config.set_password_encrypted(password)
            self.config.save()
        except ConfigError as exc:
            messagebox.showerror("保存失败", f"DPAPI 加密保存失败：{exc}")
            return
        self.var_password.set("")
        self._append_log("Windows 登录密码已通过 DPAPI 加密保存")

    # ------------------------------------------------------------- 帮助菜单

    def _open_rssi_reference(self) -> None:
        """弹出“RSSI 距离对照表”二级菜单窗口（非模态，可边看边调阈值）。"""
        top = tk.Toplevel(self.root)
        top.title("RSSI 距离对照表")
        top.geometry("660x480")
        top.transient(self.root)

        frame = ttk.Frame(top, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text="BLE RSSI 与距离的参考对照（仅供参考）",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        tree = ttk.Treeview(
            frame, columns=("distance", "rssi", "note"),
            show="headings", height=12,
        )
        tree.heading("distance", text="距离")
        tree.heading("rssi", text="参考 RSSI (dBm)")
        tree.heading("note", text="说明")
        tree.column("distance", width=80, anchor="center")
        tree.column("rssi", width=120, anchor="center")
        tree.column("note", width=400, anchor="w")
        for row in self.RSSI_DISTANCE_TABLE:
            tree.insert("", tk.END, values=row)
        tree.pack(fill="both", expand=True)

        note = (
            "参考依据：Apple iBeacon / CLProximity 官方分级（Immediate≈<0.5 m、"
            "Near≈0.5~3 m、Far≈>3 m），数值综合 BLE 实测与 n=2 对数路径损耗模型。\n"
            "注意：实际 RSSI 受设备发射功率、天线方向、人体遮挡、墙体与 2.4GHz 干扰"
            "影响，不同 iPhone 差异可达 10 dBm 以上，请用你手机的实测值校准阈值。"
        )
        ttk.Label(
            frame, text=note, foreground="#666666",
            wraplength=620, justify="left",
        ).pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------- 监听控制

    def _on_start(self) -> None:
        ok, msg = self._collect_values(save=True)
        if not ok:
            messagebox.showerror("无法启动", msg)
            return
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning("提示", "监听已在运行")
            return
        self.stop_event.clear()
        self._set_running(True)
        self.worker = threading.Thread(
            target=self._worker, daemon=True, name="BLEWorker",
        )
        self.worker.start()
        self._append_log("监听线程已启动")

    def _on_stop(self) -> None:
        self.stop_event.set()
        self._append_log("正在停止监听...")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self._update_status()

    def _worker(self) -> None:
        """后台线程：运行 asyncio 扫描主循环。"""
        try:
            asyncio.run(self._async_loop())
        except Exception:
            logger.exception("监听线程异常退出")
        finally:
            self.msg_queue.put(("stopped", None))

    async def _async_loop(self) -> None:
        """构建扫描器/控制器并运行，直到 stop_event 置位。"""
        from scanner import AdapterResetter, Scanner

        config = self.config
        try:
            resolver = create_resolver(config)
        except ResolverError as exc:
            logger.error("设备识别器初始化失败: %s", exc)
            return
        logger.info("设备识别器: %s", resolver.description)

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
        self._controller = controller
        self.msg_queue.put(("state", controller.locked))

        power_monitor = PowerMonitor(scanner.notify_wake)
        power_monitor.start()
        try:
            def on_scan(rssi: Optional[int]) -> None:
                self.msg_queue.put(("rssi", rssi))
                controller.on_scan_result(rssi)
                self.msg_queue.put(("state", controller.locked))

            await scanner.run(on_scan, self.stop_event)
        finally:
            power_monitor.stop()

    # ------------------------------------------------------------- 关闭

    def on_close(self) -> None:
        """窗口关闭：停止后台监听后退出。"""
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.worker.join(timeout=3)
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self.root.destroy()


def main(config_path: Optional[str] = None) -> int:
    """GUI 入口（供 main.py --gui 与打包 exe 调用）。

    启动阶段任何异常都会写入 %APPDATA%\\BLEAutoUnlock\\gui_error.log
    并弹窗提示，避免无控制台环境下"打不开界面却看不到错误"。
    """
    import traceback

    _write_crash_log("GUI 启动")
    try:
        root = tk.Tk()
    except Exception:
        detail = traceback.format_exc()
        _write_crash_log(f"Tk 初始化失败:\n{detail}")
        _show_error_dialog(
            "BLEAutoUnlock 启动失败",
            "图形界面初始化失败（可能是打包时缺少 Tcl/Tk 资源）。\n"
            "详情已写入 %APPDATA%\\BLEAutoUnlock\\gui_error.log",
        )
        return 1

    root.title("BLEAutoUnlock 控制面板")
    instance = SingleInstance()
    if instance.already_running:
        _write_crash_log("检测到另一个实例正在运行，本实例退出")
        messagebox.showwarning("BLEAutoUnlock", "另一个实例正在运行，本实例退出")
        root.destroy()
        return 1
    try:
        BLEAutoUnlockApp(root, config_path)
    except ConfigError as exc:
        _write_crash_log(f"配置错误: {exc}")
        messagebox.showerror("配置错误", str(exc))
        root.destroy()
        return 1
    except Exception:
        detail = traceback.format_exc()
        _write_crash_log(f"界面初始化失败:\n{detail}")
        _show_error_dialog("BLEAutoUnlock 启动失败", f"{detail}")
        root.destroy()
        return 1
    _write_crash_log("GUI 主循环已启动（窗口应已显示）")
    root.mainloop()
    instance.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

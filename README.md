# BLEAutoUnlock —— 睡眠唤醒蓝牙自动解锁工具

Windows 10/11（x64）+ Python 3.10+ 下的本地工具：电脑从睡眠（Sleep）唤醒后停留在锁屏界面时，通过检测指定 iPhone 的蓝牙信号强度（RSSI），自动解锁屏幕；iPhone 离开后自动锁定。所有逻辑全部本地实现，不依赖任何付费 API。

> 明确排除：本程序不处理"关机后首次开机"的登录界面，仅限睡眠/休眠唤醒后的用户会话解锁。

## 工作原理

```text
BLE 扫描(bleak) ──> 设备识别(resolver, IRK) ──> RSSI 状态机 ──> 解锁/锁定(actions)
      │                                                        │
      └── 睡眠唤醒恢复(异常重连 + WM_POWERBROADCAST) ───────────┘
```

- 持续扫描 BLE 广播，用 IRK 解析 iPhone 的随机可解析私有地址（RPA，MAC 每 15 分钟变化一次）。
- `RSSI >= unlock_rssi` 且持续 `unlock_hold_seconds` 秒 → 解锁。
- `RSSI < lock_rssi` 或设备消失，连续 `miss_count_before_lock` 次扫描**且累计超过 `lock_min_elapsed` 秒** → 锁定（误判防御）。
- 介于两个阈值之间为迟滞区间，保持当前状态，避免来回抖动。

## 目录结构

```text
ble_autounlock/
├── main.py              # 程序入口：--run / --install / --uninstall / --set-password / --selftest
├── scanner.py           # BLE 扫描封装（bleak）+ 适配器异常重置（devcon / PowerShell PnP / btpair）
├── resolver.py          # 设备识别基类 + iOS IRK 解析 + Android 预留
├── actions.py           # 锁屏/解锁 Windows API 封装（LockWorkStation / WTSUnlockConsole / keyboard）
├── gui.py               # tkinter 图形界面（配置 IRK/阈值/时间 + 启停监听）
├── config_manager.py    # 配置读写 + DPAPI 密码加解密
├── logger.py            # 日志配置（按天轮转，保留最近 3 天）
├── service.py           # Windows 服务封装（pywin32 win32serviceutil）
├── build.bat            # 打包 GUI exe 的一键脚本
├── BLEAutoUnlock.spec   # PyInstaller 打包配置
├── requirements.txt     # 依赖清单
├── config.example.json  # 配置文件模板（不含任何密钥）
├── README.md            # 本文件
└── tests/
    └── test_resolver.py # IRK/AES 算法向量测试
```

## 安装

1. 安装 Python 3.10+（x64），安装时勾选 **Add Python to PATH**。
2. 进入项目目录安装依赖：

```powershell
cd ble_autounlock
pip install -r requirements.txt
```

3. 运行自检确认环境与算法正常：

```powershell
python main.py --selftest
```

## 配置（config.json）

首次运行（或 `--selftest` / `--set-password`）会自动在 `main.py` 同目录生成默认 `config.json`。主要字段如下：

> 安全提醒：`config.json` 可能包含 DPAPI 加密密码与 IRK，**已被 `.gitignore` 排除**，不会进入 Git 仓库。仓库内提供 `config.example.json` 作为字段模板，首次使用可复制为 `config.json` 后填写。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `device_type` | `ios` | `ios`（IRK 随机地址）或 `android`（固定 MAC，预留） |
| `irk_key` | 空 | 64 位十六进制 IRK（32 个字符），见下文"获取 IRK" |
| `android_mac` | 空 | Android 固定 MAC（预留） |
| `windows_password` | 空 | DPAPI 加密后的密码（base64），**绝不明文**，用 `--set-password` 生成 |
| `unlock_rssi` | `-60` | RSSI 高于该值视为"靠近"，持续达标后解锁 |
| `lock_rssi` | `-75` | RSSI 低于该值或设备消失视为"离开" |
| `scan_interval` | `2` | 两次扫描之间的间隔（秒） |
| `scan_duration` | `2` | 单次扫描持续时长（秒） |
| `miss_count_before_lock` | `5` | 连续多少次扫描不到目标才考虑锁定 |
| `lock_min_elapsed` | `10` | 设备消失后累计最少经过的时间（秒），与次数同时满足才锁定 |
| `unlock_hold_seconds` | `3` | RSSI 持续达标的最短时间（秒）后才解锁 |
| `adapter_reset_retries` | `3` | 适配器异常时重置并重试的最大次数 |
| `adapter_reset_delay` | `5` | 重置适配器后的延迟（秒） |
| `irk_resolve_method` | `ble_standard` | `ble_standard`（BLE 标准 AES-128，推荐）或 `legacy_hmac`（需求伪代码变体） |
| `irk_prand_position` | 空 | `head`（prand 在地址前 3 字节）/ `tail`（后 3 字节）；留空按方法自动选择 |
| `log_level` | `INFO` | 日志级别 |
| `log_dir` | `C:\ProgramData\BLEAutoUnlock\logs` | 日志目录 |

### 获取 IRK

程序只负责使用 IRK，不负责获取。三种常用途径：

1. **ESP32 开发板**：烧录 irk-capture 固件，扫描并捕获 iPhone 广播，导出 IRK。
2. **macOS 钥匙串**：在 macOS 上登录同一 Apple ID，从钥匙串导出蓝牙 IRK（`bluetoothd` / 配对记录）。
3. **Windows 注册表（SYSTEM 权限）**：用 psexec 以 SYSTEM 权限读取 `HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys` 下的 IRK 值。

把 IRK 填进 `config.json` 的 `irk_key` 字段（32 个十六进制字符）。

### 关于随机地址字节序（重要）

bleak 在 Windows 上返回的 `device.address` 是标准 MAC 字符串（大端序），BLE 规范中可解析私有地址为 `prand(高 24 位) || hash(低 24 位)`，因此：

- 前 3 字节是 prand（最高 2 bit 固定为 `0b01`，标识 RPA）；
- 后 3 字节是 hash；
- 校验：`hash' = AES-128(key=IRK, 13个零字节 + prand)`，取密文最后 3 字节与地址后 3 字节比对。

> 泰凌微官方示例：IRK=`8b7335fd098b5e45093de94c36bf8997`、地址 `57:70:58:11:09:C9`，prand=`57:70:58` → hash=`11:09:C9`，本程序已内置该向量用于 `--selftest`。

如果从其它来源（抓包工具、其它蓝牙栈）拿到的地址字节序不同导致识别失败，可把 `irk_prand_position` 改为 `tail`（prand 在后 3 字节，即需求伪代码 `random_address[3:6]` 的线上字节序）。`irk_resolve_method=legacy_hmac` 时默认使用 `tail`，与需求附言的伪代码一致。

## 保存 Windows 登录密码

密码使用当前 Windows 用户的 DPAPI 凭据（`CryptProtectData`）加密后存入 `config.json`，不落明文：

```powershell
python main.py --set-password
```

注意：

- DPAPI 加密与**当前 Windows 用户**绑定。若改用其他账户（如 LocalSystem 服务）运行，需要在该账户下重新执行 `--set-password`。
- 换机器/重装系统后旧密文无法解密，需要重新设置。

## 运行方式

### 1. 前台调试运行

```powershell
python main.py --run
```

前台模式带控制台日志，适合先确认设备识别和 RSSI 阈值是否符合预期。按 `Ctrl+C` 退出。

### 2. 安装为 Windows 服务（需管理员）

```powershell
python main.py --install     # 安装（自动设为开机自启）
sc start BLEAutoUnlock       # 启动
python main.py --uninstall    # 卸载（需先停止服务）
```

服务无控制台窗口。**注意**：Windows 服务默认运行在 Session 0，无法访问用户交互桌面，因此"键盘输入密码解锁"在服务模式下基本无效；服务模式适合监控/锁定场景，若需要完整解锁功能请用下面的"用户登录自启"。

### 3. 用户登录自启（推荐用于完整解锁功能）

把以下命令加入"启动"文件夹（`Win+R` → `shell:startup`）或任务计划程序（登录时启动，最高权限）：

```powershell
pythonw.exe C:\path\to\ble_autounlock\main.py --run
```

使用 `pythonw.exe` 可无控制台窗口运行；任务计划程序中选择"使用最高权限运行"可提升键盘模拟解锁的成功率。

## 图形界面（GUI）

除了命令行，程序还带一个 tkinter 控制面板，可以可视化修改 IRK、蓝牙阈值、判定时间，并直接启停监听、实时查看 RSSI 与日志：

```powershell
python main.py --gui
```

面板包含：

- 设备与密钥：设备类型（ios/android）、IRK、Android MAC、Windows 登录密码（DPAPI 加密保存）
- 蓝牙阈值：解锁 RSSI、锁定 RSSI（dBm）
- 判定时间：扫描间隔、单次扫描时长、靠近持续时长、离开累计时长、连续未检测次数
- 高级选项：IRK 解析方法（ble_standard / legacy_hmac）、prand 位置
- 操作：保存配置、开始/停止监听，以及实时 RSSI、会话状态与运行日志

界面修改后点"保存配置"写入 `config.json`；点"开始监听"后后台运行扫描状态机，解锁/锁定动作与命令行模式完全一致。

## 打包为 exe

在**有网络**的 Windows 机器上双击运行 `build.bat`（或手动执行下面三条命令），即可生成单文件、无控制台窗口的 `dist\BLEAutoUnlock.exe`：

```powershell
pip install -r requirements.txt
pip install pyinstaller pyinstaller-hooks-contrib
pyinstaller --noconfirm --clean BLEAutoUnlock.spec
```

说明：

- exe 双击后直接打开图形界面；关闭窗口即退出监听。
- 打包后的配置文件位于 `%APPDATA%\BLEAutoUnlock\config.json`（不会写进 exe 所在目录，避免 Program Files 等位置无写权限）。
- 打包配置已包含 bleak 的 WinRT 后端与 pywin32 相关模块的收集规则；若杀毒软件误报，可添加信任或改用 `--onedir` 方式打包。
- 首次打包需要几分钟，exe 约几十 MB（含 Python 运行时与 BLE 依赖）。

## 日志

- 日志文件：`C:\ProgramData\BLEAutoUnlock\logs\ble_autounlock.log`，按天轮转，保留最近 3 天。
- 每 10 秒记录一次当前扫描到的目标设备 RSSI（未发现则记录"未发现"）。
- 状态变化（锁定→解锁、解锁→锁定）时强制记录。

## 自检

```powershell
python main.py --selftest
```

自检内容包括：NIST FIPS-197 C.1 / 泰凌微 / EnOcean 算法向量、配置文件、依赖（bleak / pywin32 / keyboard）、管理员权限与会话锁定状态。

## 故障排查

| 现象 | 排查建议 |
| --- | --- |
| 扫描异常"设备不可用" | 程序会自动重置蓝牙适配器并延迟 5 秒重试（最多 3 次）。重置优先用 devcon（需 WDK 的 devcon.exe，可用 `DEVCON_PATH` 环境变量指定），其次 PowerShell PnP 禁用/启用出错设备，最后 btpair 兼容入口。请确认蓝牙已开启。 |
| iPhone 识别不到 | 检查 `irk_key` 是否 32 个十六进制字符、`--selftest` 向量是否通过；用手机蓝牙设置确认 iPhone 在广播；必要时抓包核对地址字节序并调整 `irk_prand_position`。 |
| 解锁没反应 | 实测 Windows 10/11 的 `wtsapi32.dll` **不导出** `WTSUnlockConsole`，程序会自动使用键盘输入密码方案。锁屏为 Windows 安全桌面，普通权限进程的模拟输入可能被拦截，请以管理员/任务计划"最高权限"运行；确认 `windows_password` 已通过 `--set-password` 配置。 |
| 锁屏后键盘方案仍失效 | 这是 Windows 安全桌面的系统级限制。可考虑改用硬件方案（如蓝牙手环+官方动态锁），或将本程序作为辅助手段并保留手动输入。 |
| 服务模式下无法解锁 | 服务运行在 Session 0，无交互桌面；改用"用户登录自启"方式。 |
| RSSI 波动导致频繁锁定/解锁 | 调大 `lock_rssi` 与 `unlock_rssi` 的间隔（迟滞区），或增大 `miss_count_before_lock` / `lock_min_elapsed`。 |

## 已知限制与安全说明

- Windows 锁屏是安全桌面（Winlogon），普通进程无法直接注入按键；键盘解锁方案的成功率取决于进程权限与系统版本，**请先实测再依赖它**。
- `WTSUnlockConsole` 在当前 Windows 版本不存在，代码保留调用探测，实际走键盘备用方案。
- 密码仅用 DPAPI 加密保存在本机，密钥与当前用户绑定；请勿分享 `config.json`。
- 蓝牙 RSSI 受环境干扰，阈值建议结合日志实测调整。
- 本程序仅处理睡眠/休眠唤醒后的锁屏解锁，不处理首次开机登录界面。

注意：解锁屏幕功能仍在开发中，自动锁屏功能已推出。

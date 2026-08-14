"""IRK / AES 解析算法测试（可独立运行，也可由 main.py --selftest 调用）。

覆盖向量：
  1. NIST FIPS-197 C.1：验证最小 AES-128 实现的正确性
  2. 泰凌微 TLSR8258 私有地址解析示例（BLE 标准 RPA）
  3. EnOcean PTM 216B 手册示例（BLE 标准 RPA）
  4. legacy_hmac 需求伪代码分支（构造性验证）

运行方式：
  python tests/test_resolver.py
"""

from __future__ import annotations

import hashlib
import hmac
import sys

sys.path.insert(0, sys.path[0] + "/..")  # 允许直接运行本文件

from resolver import _AES128, IOSResolver  # noqa: E402


def run_vectors() -> bool:
    """运行全部向量；全部通过返回 True。"""
    ok = True

    # -------------------------------------------------------- 1) NIST C.1
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    plaintext = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    expected = bytes.fromhex("3ad77bb40d7a3660a89ecaf32466ef97")
    actual = _AES128(key).encrypt_block(plaintext)
    if actual == expected:
        print("  [OK] NIST FIPS-197 C.1 AES-128")
    else:
        print(f"  [FAIL] NIST FIPS-197 C.1: got {actual.hex()}, want {expected.hex()}")
        ok = False

    # ------------------------------------------------ 2) 泰凌微 RPA 示例
    resolver = IOSResolver("8b7335fd098b5e45093de94c36bf8997")
    # 地址 57:70:58:11:09:C9，prand=57:70:58，hash=11:09:C9
    if resolver.matches("57:70:58:11:09:C9"):
        print("  [OK] 泰凌微 RPA 解析示例")
    else:
        print("  [FAIL] 泰凌微 RPA 解析示例：合法地址未被识别")
        ok = False

    # 错误 IRK 不应匹配
    wrong = IOSResolver("8b7335fd098b5e45093de94c36bf8998")
    if not wrong.matches("57:70:58:11:09:C9"):
        print("  [OK] 错误 IRK 反例")
    else:
        print("  [FAIL] 错误 IRK 反例：错误密钥竟然匹配成功")
        ok = False

    # ------------------------------------------------ 3) EnOcean 示例
    resolver_eno = IOSResolver("be759a027a4870fd242794f4c45220fb")
    if resolver_eno.matches("49:39:70:E5:19:44"):
        print("  [OK] EnOcean PTM 216B RPA 示例")
    else:
        print("  [FAIL] EnOcean PTM 216B RPA 示例")
        ok = False

    # ------------------------------------------------ 4) legacy_hmac 分支
    irk = bytes.fromhex("8b7335fd098b5e45093de94c36bf8997")
    prand = b"\x57\x70\x58"
    digest = hmac.new(irk, prand + b"btle", hashlib.sha256).digest()
    # tail 模式下 prand 在地址 [3:6]，hash 字段在 [0:3]（与需求伪代码一致）
    wire_addr = digest[:3] + prand
    addr_str = ":".join(f"{b:02X}" for b in wire_addr)
    legacy = IOSResolver("8b7335fd098b5e45093de94c36bf8997",
                         method="legacy_hmac")
    if legacy.matches(addr_str):
        print("  [OK] legacy_hmac 需求伪代码分支")
    else:
        print(f"  [FAIL] legacy_hmac 分支：地址 {addr_str} 未匹配")
        ok = False

    # 同地址不应被 ble_standard 匹配（两种算法结果不同）
    standard = IOSResolver("8b7335fd098b5e45093de94c36bf8997")
    if not standard.matches(addr_str):
        print("  [OK] 跨算法反例（legacy_hmac 地址不匹配 ble_standard）")
    else:
        print("  [FAIL] 跨算法反例：ble_standard 错误匹配 legacy 地址")
        ok = False

    return ok


if __name__ == "__main__":
    sys.exit(0 if run_vectors() else 1)

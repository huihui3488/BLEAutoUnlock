"""设备识别模块。

- BaseResolver：抽象基类，所有识别器都实现 matches(address, rssi)
- IOSResolver：使用 IRK（Identity Resolving Key）解析 iPhone 的
  随机可解析私有地址（Resolvable Private Address, RPA）
  * ble_standard（推荐，BLE 标准算法）：
      r' = 13 个零字节 + prand
      hash = AES-128-ECB(key=IRK, data=r') 的最低 24 位（即密文最后 3 字节）
      与地址中的 hash 字段比对，一致则认为是目标设备
  * legacy_hmac（需求附言给出的 HMAC 变体）：
      hmac(irk, prand + b"btle") 取前 3 字节比对
- AndroidResolver：预留的固定 MAC 匹配实现

字节序约定（重要）：
    bleak 在 Windows（WinRT 后端）上返回的 device.address 是标准 MAC
    字符串（大端序，例如 "57:70:58:11:09:C9"）。BLE 规范中 RPA 的 48 位
    地址 = prand(高 24 位) || hash(低 24 位)，因此字符串中：
      - 前 3 字节为 prand（最高 2 bit 固定为 0b01，标识可解析私有地址）
      - 后 3 字节为 hash
    prand_position="head"（默认）即按上述大端序处理；
    prand_position="tail" 表示 prand 在后 3 字节（即按线上字节序/需求
    伪代码 random_address[3:6] 的方式处理），供其它字节序来源使用。

参考测试向量（与 Bluetooth Core Spec Vol 6 Part B 1.3.2 一致）：
    - 泰凌微 TLSR8258 示例：IRK=8b7335fd098b5e45093de94c36bf8997,
      prand=577058 -> hash=1109c9
    - EnOcean PTM 216B 手册示例：IRK=be759a027a4870fd242794f4c45220fb,
      prand=493970 -> hash=e51944

AES-128 为最小纯 Python 实现（仅加密单个分组），避免引入额外依赖；
其正确性由 tests/test_resolver.py 中的 NIST FIPS-197 C.1 向量与上述
参考向量共同保证。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger(__name__)


class ResolverError(Exception):
    """设备识别相关错误。"""


# ================================================================ AES-128


class _AES128:
    """最小 AES-128 实现（仅支持 128 位密钥、单分组 ECB 加密）。

    仅供 BLE IRK 解析使用，不用于任何数据加密/传输场景。
    """

    _SBOX: Optional[List[int]] = None
    _RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

    @staticmethod
    def _rotl8(value: int, bits: int) -> int:
        """8 位循环左移。"""
        return ((value << bits) | (value >> (8 - bits))) & 0xFF

    @classmethod
    def _build_sbox(cls) -> List[int]:
        """按 Rijndael 定义动态生成 S 盒（避免手抄 256 项，便于审计）。"""
        sbox = [0] * 256
        p = q = 1
        while True:
            # p 在 GF(2^8) 中乘以 3（即乘以 x+1，0x1B 为不可约多项式的低 8 位）
            p = p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)
            p &= 0xFF
            # q 在 GF(2^8) 中除以 3（等价于乘以 0xF6，来自 Rijndael 经典生成法）
            q ^= q << 1
            q ^= q << 2
            q ^= q << 4
            q ^= 0x09 if q & 0x80 else 0
            q &= 0xFF
            # 仿射变换：q ^ rotl8(q,1) ^ rotl8(q,2) ^ rotl8(q,3) ^ rotl8(q,4)
            xformed = (q ^ cls._rotl8(q, 1) ^ cls._rotl8(q, 2)
                       ^ cls._rotl8(q, 3) ^ cls._rotl8(q, 4))
            sbox[p] = xformed ^ 0x63
            if p == 1:
                break
        sbox[0] = 0x63
        return sbox

    @classmethod
    def _sbox(cls) -> List[int]:
        if cls._SBOX is None:
            cls._SBOX = cls._build_sbox()
        return cls._SBOX

    def __init__(self, key: bytes):
        if len(key) != 16:
            raise ValueError("AES-128 密钥必须是 16 字节")
        self._round_keys = self._expand_key(key)

    @classmethod
    def _expand_key(cls, key: bytes) -> List[List[int]]:
        """密钥扩展：生成 44 个字（11 轮密钥）。"""
        sbox = cls._sbox()
        words = [list(key[4 * i: 4 * i + 4]) for i in range(4)]
        for i in range(4, 44):
            temp = list(words[i - 1])
            if i % 4 == 0:
                temp = temp[1:] + temp[:1]            # RotWord
                temp = [sbox[b] for b in temp]        # SubWord
                temp[0] ^= cls._RCON[i // 4 - 1]      # Rcon
            words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
        return words

    @staticmethod
    def _xtime(value: int) -> int:
        """GF(2^8) 中乘以 x（不可约多项式 0x11B）。"""
        value <<= 1
        if value & 0x100:
            value ^= 0x11B
        return value & 0xFF

    def encrypt_block(self, block: bytes) -> bytes:
        """加密单个 16 字节分组（ECB），返回 16 字节密文。"""
        if len(block) != 16:
            raise ValueError("AES 分组必须是 16 字节")
        sbox = self._sbox()
        # AES 状态按列存储：state[row][col] = block[row + 4*col]
        state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

        def add_round_key(round_index: int) -> None:
            for c in range(4):
                word = self._round_keys[round_index * 4 + c]
                for r in range(4):
                    state[r][c] ^= word[r]

        add_round_key(0)
        for rnd in range(1, 10):
            # SubBytes
            for r in range(4):
                for c in range(4):
                    state[r][c] = sbox[state[r][c]]
            # ShiftRows
            for r in range(4):
                state[r] = state[r][r:] + state[r][:r]
            # MixColumns
            for c in range(4):
                t0, t1, t2, t3 = (state[r][c] for r in range(4))
                state[0][c] = self._xtime(t0) ^ (self._xtime(t1) ^ t1) ^ t2 ^ t3
                state[1][c] = t0 ^ self._xtime(t1) ^ (self._xtime(t2) ^ t2) ^ t3
                state[2][c] = t0 ^ t1 ^ self._xtime(t2) ^ (self._xtime(t3) ^ t3)
                state[3][c] = (self._xtime(t0) ^ t0) ^ t1 ^ t2 ^ self._xtime(t3)
            add_round_key(rnd)
        # 最后一轮（无 MixColumns）
        for r in range(4):
            for c in range(4):
                state[r][c] = sbox[state[r][c]]
        for r in range(4):
            state[r] = state[r][r:] + state[r][:r]
        add_round_key(10)

        out = bytearray(16)
        for r in range(4):
            for c in range(4):
                out[r + 4 * c] = state[r][c]
        return bytes(out)


# ================================================================ 地址工具


def normalize_address(address: str) -> Optional[bytes]:
    """把 "AA:BB:CC:DD:EE:FF" 或 "AABBCCDDEEFF" 转为 6 字节 bytes；非法返回 None。"""
    if not isinstance(address, str):
        return None
    compact = address.replace(":", "").replace("-", "").strip()
    if len(compact) != 12:
        return None
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return None


def _address_to_str(address_bytes: bytes) -> str:
    return ":".join(f"{b:02X}" for b in address_bytes)


# ================================================================ 识别器


class BaseResolver(ABC):
    """设备识别抽象基类。"""

    @abstractmethod
    def matches(self, address: str, rssi: Optional[int] = None) -> bool:
        """判断给定 MAC 地址（及可选 RSSI）是否为目标设备。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """人类可读的识别器描述（用于日志/自检）。"""


class IOSResolver(BaseResolver):
    """iOS 设备识别：用 IRK 解析 iPhone 的随机可解析私有地址。"""

    def __init__(self, irk_key: str,
                 method: str = "ble_standard",
                 prand_position: Optional[str] = None):
        """初始化 IRK 解析器。

        :param irk_key: 64 位十六进制 IRK 密钥（32 个十六进制字符）
        :param method: ble_standard（BLE 标准 AES-128）或 legacy_hmac
        :param prand_position: head（prand 在地址前 3 字节，默认，
            适用于 bleak 返回的大端序 MAC 字符串）/ tail（prand 在
            后 3 字节，与需求伪代码 random_address[3:6] 一致）。
            为 None 时按方法取默认：ble_standard -> head，
            legacy_hmac -> tail。
        """
        irk_hex = str(irk_key or "").strip().lower().replace("0x", "")
        if len(irk_hex) != 32:
            raise ResolverError("irk_key 必须是 64 位十六进制字符串（32 个字符）")
        try:
            self.irk = bytes.fromhex(irk_hex)
        except ValueError as exc:
            raise ResolverError("irk_key 包含非法十六进制字符") from exc

        self.method = str(method or "ble_standard").lower()
        if self.method not in ("ble_standard", "legacy_hmac"):
            raise ResolverError(f"不支持的 irk_resolve_method: {self.method}")

        if prand_position is None or not str(prand_position).strip():
            # 未显式配置时：legacy_hmac 按需求伪代码取 tail，标准算法取 head
            self.prand_position = "tail" if self.method == "legacy_hmac" else "head"
        else:
            self.prand_position = str(prand_position).strip().lower()
        if self.prand_position not in ("head", "tail"):
            raise ResolverError(f"不支持的 irk_prand_position: {self.prand_position}")

    @property
    def description(self) -> str:
        return f"iOS (IRK, 方法={self.method}, prand位置={self.prand_position})"

    def _prand_and_hash(self, addr: bytes) -> tuple[bytes, bytes]:
        """按配置的字节序拆出 prand 与 hash 字段。"""
        if self.prand_position == "head":
            return addr[0:3], addr[3:6]
        return addr[3:6], addr[0:3]

    def _is_resolvable_private(self, addr: bytes) -> bool:
        """判断是否为可解析私有地址（RPA）。

        RPA 的 prand 最高 2 bit 固定为 0b01；公共地址/静态随机地址等
        一律排除，避免对非随机地址做无意义的 IRK 计算。
        """
        prand, _hash_field = self._prand_and_hash(addr)
        return bool(prand and (prand[0] & 0xC0) == 0x40)

    def resolve_identity(self, irk: bytes, random_address: bytes) -> bool:
        """判断 random_address 是否由 irk 派生的可解析私有地址。

        :param irk: 16 字节 IRK
        :param random_address: 6 字节随机地址（bytes，字节序见类注释）
        """
        if len(irk) != 16 or len(random_address) != 6:
            return False
        if self.method == "legacy_hmac":
            return self._irk_matches_legacy_hmac(irk, random_address)
        return self._irk_matches_ble_standard(irk, random_address)

    def _irk_matches_ble_standard(self, irk: bytes, addr: bytes) -> bool:
        """BLE 标准算法：hash = AES-128(key=IRK, 13个零字节 + prand) 的最低 24 位。"""
        prand, hash_field = self._prand_and_hash(addr)
        r_prime = b"\x00" * 13 + prand
        digest = _AES128(irk).encrypt_block(r_prime)
        return digest[-3:] == hash_field

    def _irk_matches_legacy_hmac(self, irk: bytes, addr: bytes) -> bool:
        """需求伪代码的 HMAC 变体：hmac(irk, prand + b"btle") 前 3 字节比对。"""
        prand, hash_field = self._prand_and_hash(addr)
        digest = hmac.new(irk, prand + b"btle", hashlib.sha256).digest()
        return digest[:3] == hash_field

    def matches(self, address: str, rssi: Optional[int] = None) -> bool:
        addr = normalize_address(address)
        if addr is None or not self._is_resolvable_private(addr):
            return False
        return self.resolve_identity(self.irk, addr)


class AndroidResolver(BaseResolver):
    """Android 预留实现：按固定 MAC 匹配（后续可扩展 Android 私有协议）。"""

    def __init__(self, mac: str):
        if not mac:
            raise ResolverError("device_type=android 时需要配置 android_mac")
        self.target = normalize_address(mac)
        if self.target is None:
            raise ResolverError(f"android_mac 格式非法: {mac}")

    @property
    def description(self) -> str:
        return f"Android (固定 MAC: {_address_to_str(self.target)})"

    def matches(self, address: str, rssi: Optional[int] = None) -> bool:
        addr = normalize_address(address)
        return addr is not None and addr == self.target


def create_resolver(config) -> BaseResolver:
    """根据配置构建设备识别器。"""
    device_type = str(config.get("device_type", "ios")).strip().lower()
    if device_type == "ios":
        irk_key = config.get("irk_key", "") or ""
        if not str(irk_key).strip():
            raise ResolverError("config.json 缺少 irk_key（64 位十六进制 IRK）")
        return IOSResolver(
            str(irk_key),
            method=config.get("irk_resolve_method", "ble_standard"),
            prand_position=config.get("irk_prand_position"),
        )
    if device_type == "android":
        return AndroidResolver(str(config.get("android_mac", "") or ""))
    raise ResolverError(f"不支持的 device_type: {device_type}")

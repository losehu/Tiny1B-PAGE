#!/usr/bin/env python3
"""
ARM-friendly Tiny1B UVC vendor control command tool.

Reverse-mapped from tiny1B linux x86_64 libiruvc.so:
- bmRequestType IN  = 0xC1, bRequest = 0x19
- bmRequestType OUT = 0x41, bRequest = 0x20
- wValue = command id
- wIndex = parameter / address selector
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import re
import struct
import sys
from dataclasses import dataclass
from typing import Optional


# ioctl encoding (asm-generic)
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_DIRBITS = 2

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, ioc_type: int, nr: int, size: int) -> int:
    return (
        ((direction & ((1 << _IOC_DIRBITS) - 1)) << _IOC_DIRSHIFT)
        | ((ioc_type & ((1 << _IOC_TYPEBITS) - 1)) << _IOC_TYPESHIFT)
        | ((nr & ((1 << _IOC_NRBITS) - 1)) << _IOC_NRSHIFT)
        | ((size & ((1 << _IOC_SIZEBITS) - 1)) << _IOC_SIZESHIFT)
    )


def _iowr(ioc_type: int, nr: int, size: int) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, ioc_type, nr, size)


class UsbdevfsCtrlTransfer(ctypes.Structure):
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


USBDEVFS_CONTROL = _iowr(ord("U"), 0, ctypes.sizeof(UsbdevfsCtrlTransfer))


BMREQ_IN = 0xC1
BMREQ_OUT = 0x41
BREQ_READ = 0x19
BREQ_WRITE = 0x20

CTRL_TIMEOUT_MS = 1000
STD_READ_MAX_CHUNK = 0xFF
STD_WRITE_MAX_CHUNK = 0x1F
SHORT_READ_MAX_CHUNK = 0x20


def _parse_int(value: str) -> int:
    return int(value, 0)


def _parse_hex_bytes(value: str) -> bytes:
    s = re.sub(r"[^0-9a-fA-F]", "", value or "")
    if not s:
        return b""
    if len(s) % 2 != 0:
        raise ValueError("hex data length must be even")
    return bytes.fromhex(s)


def _u16(data: bytes) -> int:
    return struct.unpack("<H", data)[0]


def _s16(data: bytes) -> int:
    return struct.unpack("<h", data)[0]


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _find_usb_parent(sys_device_path: str) -> Optional[str]:
    cur = os.path.realpath(sys_device_path)
    while True:
        if all(
            os.path.exists(os.path.join(cur, name))
            for name in ("busnum", "devnum", "idVendor", "idProduct")
        ):
            return cur
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return None
        cur = nxt


def _parse_interface_num(path: str) -> Optional[int]:
    base = os.path.basename(path)
    # e.g. "3-1:1.0" -> interface 0
    m = re.search(r":\d+\.(\d+)$", base)
    if not m:
        return None
    return int(m.group(1))


@dataclass
class UsbEndpoint0Path:
    video_device: str
    video_sys_path: str
    usb_sys_path: str
    usb_busnum: int
    usb_devnum: int
    usb_bus_path: str
    vid: int
    pid: int
    interface_num: Optional[int]


def resolve_usb_path_from_video(video_device: str) -> UsbEndpoint0Path:
    video_name = os.path.basename(video_device)
    if not video_name.startswith("video"):
        raise RuntimeError(f"invalid video device path: {video_device}")

    video_sys = f"/sys/class/video4linux/{video_name}/device"
    if not os.path.exists(video_sys):
        raise RuntimeError(f"missing sysfs node: {video_sys}")

    video_sys_real = os.path.realpath(video_sys)
    usb_sys = _find_usb_parent(video_sys_real)
    if not usb_sys:
        raise RuntimeError(f"cannot locate USB parent from {video_sys_real}")

    busnum = int(_read_text(os.path.join(usb_sys, "busnum")))
    devnum = int(_read_text(os.path.join(usb_sys, "devnum")))
    vid = int(_read_text(os.path.join(usb_sys, "idVendor")), 16)
    pid = int(_read_text(os.path.join(usb_sys, "idProduct")), 16)
    bus_path = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
    if not os.path.exists(bus_path):
        raise RuntimeError(f"usb bus node missing: {bus_path}")

    return UsbEndpoint0Path(
        video_device=video_device,
        video_sys_path=video_sys_real,
        usb_sys_path=usb_sys,
        usb_busnum=busnum,
        usb_devnum=devnum,
        usb_bus_path=bus_path,
        vid=vid,
        pid=pid,
        interface_num=_parse_interface_num(video_sys_real),
    )


class Tiny1BUvcCtrl:
    def __init__(self, ep0: UsbEndpoint0Path):
        self._ep0 = ep0

    @property
    def endpoint_info(self) -> dict:
        return {
            "video_device": self._ep0.video_device,
            "usb_bus_path": self._ep0.usb_bus_path,
            "vid": f"0x{self._ep0.vid:04x}",
            "pid": f"0x{self._ep0.pid:04x}",
            "interface_num": self._ep0.interface_num,
            "usb_sys_path": self._ep0.usb_sys_path,
        }

    def _control_transfer(
        self,
        request_type: int,
        request: int,
        w_value: int,
        w_index: int,
        data: bytes,
        is_read: bool,
    ) -> bytes:
        payload = data if data is not None else b""
        expected_len = len(payload)
        # libiruvc behavior: zero-length transfers are sent as length=1 dummy byte.
        if expected_len == 0:
            payload = b"\x00"

        c_buf = ctypes.create_string_buffer(payload, len(payload))
        xfer = UsbdevfsCtrlTransfer(
            bRequestType=request_type & 0xFF,
            bRequest=request & 0xFF,
            wValue=w_value & 0xFFFF,
            wIndex=w_index & 0xFFFF,
            wLength=len(payload),
            timeout=CTRL_TIMEOUT_MS,
            data=ctypes.addressof(c_buf),
        )

        fd = os.open(self._ep0.usb_bus_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            actual = fcntl.ioctl(fd, USBDEVFS_CONTROL, xfer)
        finally:
            os.close(fd)

        if actual != len(payload):
            raise RuntimeError(
                "control transfer length mismatch "
                f"(actual={actual}, expected={len(payload)}, "
                f"bmReq=0x{request_type:02x}, bReq=0x{request:02x}, "
                f"wValue=0x{w_value:04x}, wIndex=0x{w_index:04x})"
            )

        if is_read:
            # Ignore dummy byte for zero-length logical read.
            return bytes(c_buf.raw[:expected_len])
        return b""

    def _read_chunk(self, cmd: int, index: int, length: int) -> bytes:
        # std_usb_data_read maps args as:
        #   bRequest = 0x19
        #   wValue   = index
        #   wIndex   = cmd
        return self._control_transfer(BMREQ_IN, BREQ_READ, index, cmd, b"\x00" * length, True)

    def _write_chunk(self, cmd: int, index: int, payload: bytes) -> None:
        # std_usb_data_write maps args as:
        #   bRequest = 0x20
        #   wValue   = index
        #   wIndex   = cmd
        self._control_transfer(BMREQ_OUT, BREQ_WRITE, index, cmd, payload, False)

    def standard_read(self, cmd: int, index: int, length: int) -> bytes:
        if length < 0:
            raise ValueError("length must be >= 0")
        if length == 0:
            return b""

        out = bytearray()
        remaining = length
        while remaining > 0:
            n = min(remaining, STD_READ_MAX_CHUNK)
            out.extend(self._read_chunk(cmd, index, n))
            remaining -= n
        return bytes(out)

    def standard_write(self, cmd: int, index: int, payload: bytes) -> None:
        data = payload or b""
        if len(data) == 0:
            self._write_chunk(cmd, index, b"")
            return

        offset = 0
        total = len(data)
        while offset < total:
            n = min(total - offset, STD_WRITE_MAX_CHUNK)
            self._write_chunk(cmd, index, data[offset : offset + n])
            offset += n

    def short_data_read(self, cmd_hi: int, start_index: int, length: int) -> bytes:
        if length < 0:
            raise ValueError("length must be >= 0")
        if length == 0:
            return b""

        out = bytearray()
        remaining = length
        idx = start_index & 0xFFFF
        while remaining > 0:
            n = min(remaining, SHORT_READ_MAX_CHUNK)
            encoded = ((cmd_hi & 0xFF) << 8) | (n & 0xFF)
            # tiny1b_short_data_rd maps as:
            #   wValue = current addr
            #   wIndex = (cmd_hi << 8) | chunk_len
            out.extend(self._control_transfer(BMREQ_IN, BREQ_READ, idx, encoded, b"\x00" * n, True))
            idx = (idx + n) & 0xFFFF
            remaining -= n
        return bytes(out)

    # Tiny1B wrappers
    def get_ir_sensor_vtemp(self) -> dict:
        data = self.standard_read(0x0181, 0x0200, 2)
        return {
            "raw_u16": _u16(data),
            "raw_s16": _s16(data),
            "bytes_hex": data.hex(),
            # Not direct Celsius. This is sensor Vtemp raw value.
            "note": "raw vtemp value from cmd 0x181 idx 0x0200",
        }

    def get_ir_sensor_flag(self) -> int:
        return self.standard_read(0x018A, 0x0100, 1)[0]

    def tpd_get_env_param(self, param: int) -> dict:
        if param in (0x0100, 0x0101):
            n = 1
        elif param in (0x0202, 0x0203):
            n = 2
        else:
            n = 0
        data = self.standard_read(0x068F, param & 0xFFFF, n)
        result = {
            "param": f"0x{param & 0xFFFF:04x}",
            "length": n,
            "hex": data.hex(),
            "raw_u16": None,
            "decoded": None,
            "note": "raw value only (device-specific scale; not guaranteed Celsius)",
        }
        if n == 1:
            v = data[0]
            result["raw_u16"] = v
        elif n == 2:
            v = _u16(data)
            result["raw_u16"] = v
        return result

    def shutter_manual(self) -> None:
        self.standard_write(0x0345, 0x0000, b"")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tiny1B ARM UVC vendor command tool (no x86 libiruvc.so dependency)"
    )
    p.add_argument(
        "-d",
        "--device",
        default="/dev/video0",
        help="video device path (default: /dev/video0)",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print JSON output",
    )
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("info", help="show resolved USB path and ids")
    sub.add_parser("vtemp", help="read IR sensor vtemp raw value")
    sub.add_parser("sensor-flag", help="read IR sensor gain flag")
    sub.add_parser("shutter-manual", help="trigger manual shutter command")

    env = sub.add_parser("env-get", help="read environment parameter via cmd 0x68f")
    env.add_argument(
        "--param",
        required=True,
        help="param key: ems|tau|ta|tu or numeric (e.g. 0x0202)",
    )

    rd = sub.add_parser("cmd-read", help="generic tiny1b_standard_cmd_read")
    rd.add_argument("--cmd-id", required=True, help="tiny1b cmd id, e.g. 0x181")
    rd.add_argument("--index", required=True, help="tiny1b index/param, e.g. 0x200")
    rd.add_argument("--length", required=True, type=_parse_int, help="read length")

    wr = sub.add_parser("cmd-write", help="generic tiny1b_standard_cmd_write")
    wr.add_argument("--cmd-id", required=True, help="tiny1b cmd id, e.g. 0x345")
    wr.add_argument("--index", required=True, help="tiny1b index/param, e.g. 0x0000")
    wr.add_argument(
        "--data",
        default="",
        help='hex payload bytes, e.g. "01 02 03" or "010203"; empty means no payload',
    )

    srd = sub.add_parser("short-read", help="generic tiny1b_short_data_rd")
    srd.add_argument("--cmd-hi", required=True, help="high byte command, e.g. 0xC2")
    srd.add_argument("--addr", required=True, help="start address, e.g. 0")
    srd.add_argument("--length", required=True, type=_parse_int, help="read length")

    kt = sub.add_parser("kt-get", help="tpd_kt_get wrapper (cmd-hi=0xC2)")
    kt.add_argument("--addr", default="0", help="start address")
    kt.add_argument("--length", required=True, type=_parse_int, help="byte length")

    nuct = sub.add_parser("nuct-get", help="tpd_nuct_get wrapper (cmd-hi=0xC3)")
    nuct.add_argument("--addr", default="0", help="start address")
    nuct.add_argument("--length", required=True, type=_parse_int, help="byte length")

    return p


def _map_env_param(key: str) -> int:
    k = key.lower()
    mapping = {
        "ems": 0x0100,
        "tau": 0x0101,
        "ta": 0x0202,
        "tu": 0x0203,
    }
    if k in mapping:
        return mapping[k]
    return int(key, 0)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        ep0 = resolve_usb_path_from_video(args.device)
        dev = Tiny1BUvcCtrl(ep0)
        out = {"ok": True, "cmd": args.action, "endpoint": dev.endpoint_info}

        if args.action == "info":
            pass
        elif args.action == "vtemp":
            out["result"] = dev.get_ir_sensor_vtemp()
        elif args.action == "sensor-flag":
            out["result"] = {"gain_flag": dev.get_ir_sensor_flag()}
        elif args.action == "shutter-manual":
            dev.shutter_manual()
            out["result"] = {"message": "shutter manual command sent"}
        elif args.action == "env-get":
            param = _map_env_param(args.param)
            out["result"] = dev.tpd_get_env_param(param)
        elif args.action == "cmd-read":
            cmd = int(args.cmd_id, 0)
            index = int(args.index, 0)
            data = dev.standard_read(cmd, index, args.length)
            out["result"] = {"cmd": f"0x{cmd:04x}", "index": f"0x{index:04x}", "hex": data.hex(), "length": len(data)}
        elif args.action == "cmd-write":
            cmd = int(args.cmd_id, 0)
            index = int(args.index, 0)
            payload = _parse_hex_bytes(args.data)
            dev.standard_write(cmd, index, payload)
            out["result"] = {
                "cmd": f"0x{cmd:04x}",
                "index": f"0x{index:04x}",
                "written_length": len(payload),
                "written_hex": payload.hex(),
            }
        elif args.action == "short-read":
            cmd_hi = int(args.cmd_hi, 0)
            addr = int(args.addr, 0)
            data = dev.short_data_read(cmd_hi, addr, args.length)
            out["result"] = {"hex": data.hex(), "length": len(data)}
        elif args.action == "kt-get":
            addr = int(args.addr, 0)
            data = dev.short_data_read(0xC2, addr, args.length)
            out["result"] = {"hex": data.hex(), "length": len(data)}
        elif args.action == "nuct-get":
            addr = int(args.addr, 0)
            data = dev.short_data_read(0xC3, addr, args.length)
            out["result"] = {"hex": data.hex(), "length": len(data)}
        else:
            raise RuntimeError(f"unsupported cmd: {args.action}")
    except Exception as exc:
        out = {"ok": False, "error": str(exc)}
        print(json.dumps(out, ensure_ascii=False, indent=2 if args.pretty else None))
        return 1

    print(json.dumps(out, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# TaishanPi WiFi status LED daemon
# - Connected to SSID "CVPU"   -> Blue solid
# - Connected to SSID "losehu" -> Red solid
# - Connected to other SSID    -> Green solid
# - Not connected              -> Blink R then G then B (repeat)

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import Optional


SYS_LEDS = "/sys/class/leds"


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _run(cmd: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class RgbLeds:
    def __init__(self, led_r: str, led_g: str, led_b: str):
        self.led_r = led_r
        self.led_g = led_g
        self.led_b = led_b
        self._init_done = False

    def _led_path(self, name: str, leaf: str) -> str:
        return os.path.join(SYS_LEDS, name, leaf)

    def _ensure_exists(self) -> None:
        missing = []
        for n in (self.led_r, self.led_g, self.led_b):
            if not os.path.isdir(os.path.join(SYS_LEDS, n)):
                missing.append(n)
        if missing:
            raise RuntimeError(
                "找不到 LED 节点："
                + ", ".join(missing)
                + f"（当前 {SYS_LEDS} 下有：{', '.join(sorted(os.listdir(SYS_LEDS)))}）"
            )

    def init(self) -> None:
        if self._init_done:
            return
        self._ensure_exists()
        # Disable triggers so brightness is controllable.
        for n in (self.led_r, self.led_g, self.led_b):
            trig = self._led_path(n, "trigger")
            if os.path.exists(trig):
                _write_text(trig, "none")
        # Mark initialized before calling helpers that may re-enter init().
        self._init_done = True
        self.off()

    def set_rgb(self, r: int, g: int, b: int) -> None:
        self.init()
        _write_text(self._led_path(self.led_r, "brightness"), "1" if r else "0")
        _write_text(self._led_path(self.led_g, "brightness"), "1" if g else "0")
        _write_text(self._led_path(self.led_b, "brightness"), "1" if b else "0")

    def off(self) -> None:
        self.set_rgb(0, 0, 0)


def _ssid_from_iwgetid() -> str:
    if not _which("iwgetid"):
        return ""
    p = _run(["iwgetid", "-r"], timeout=1.5)
    ssid = (p.stdout or "").strip()
    return ssid


def _parse_iw_dev_interfaces(iw_dev_out: str) -> list[str]:
    ifaces: list[str] = []
    for line in iw_dev_out.splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            ifaces.append(line.split(" ", 1)[1].strip())
    return ifaces


def _ssid_from_iw() -> str:
    if not _which("iw"):
        return ""
    p = _run(["iw", "dev"], timeout=2.0)
    if p.returncode != 0:
        return ""

    for iface in _parse_iw_dev_interfaces(p.stdout or ""):
        link = _run(["iw", "dev", iface, "link"], timeout=2.0)
        out = (link.stdout or "")
        if "Not connected" in out:
            continue
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                return line.split(":", 1)[1].strip()
    return ""


def _ssid_from_nmcli() -> str:
    if not _which("nmcli"):
        return ""
    # Find active SSID if NetworkManager is used.
    p = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=2.5)
    if p.returncode != 0:
        return ""
    for line in (p.stdout or "").splitlines():
        # yes:<ssid>
        if not line.startswith("yes:"):
            continue
        return line.split(":", 1)[1]
    return ""


def _ssid_from_wpa_cli() -> str:
    if not _which("wpa_cli"):
        return ""

    # Try all common wireless interfaces.
    candidates = ["wlan0", "wlan1", "wlp1s0", "wlp2s0"]
    # Add from /proc/net/wireless if present.
    try:
        with open("/proc/net/wireless", "r", encoding="utf-8") as f:
            for line in f.read().splitlines()[2:]:
                if ":" in line:
                    candidates.append(line.split(":", 1)[0].strip())
    except Exception:
        pass

    for iface in list(dict.fromkeys(candidates)):
        p = _run(["wpa_cli", "-i", iface, "status"], timeout=2.0)
        if p.returncode != 0:
            continue
        ssid = ""
        wpa_state = ""
        for line in (p.stdout or "").splitlines():
            if line.startswith("wpa_state="):
                wpa_state = line.split("=", 1)[1].strip()
            if line.startswith("ssid="):
                ssid = line.split("=", 1)[1].strip()
        if wpa_state == "COMPLETED" and ssid:
            return ssid
    return ""


def get_connected_ssid() -> str:
    # Prefer iwgetid as it's the simplest.
    for fn in (_ssid_from_iwgetid, _ssid_from_iw, _ssid_from_nmcli, _ssid_from_wpa_cli):
        try:
            ssid = fn()
        except Exception:
            ssid = ""
        if ssid:
            return ssid
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="TaishanPi WiFi LED status daemon")
    parser.add_argument("--ssid-blue", default="CVPU", help="SSID that turns blue solid")
    parser.add_argument("--ssid-red", default="losehu", help="SSID that turns red solid")
    parser.add_argument("--poll-sec", type=float, default=2.0, help="WiFi status polling interval")
    parser.add_argument("--blink-on-sec", type=float, default=0.18, help="Disconnected blink on time")
    parser.add_argument("--blink-off-sec", type=float, default=0.18, help="Disconnected blink off time")
    parser.add_argument("--blink-gap-sec", type=float, default=0.25, help="Disconnected gap after RGB sequence")
    parser.add_argument("--led-r", default="rgb-led-r")
    parser.add_argument("--led-g", default="rgb-led-g")
    parser.add_argument("--led-b", default="rgb-led-b")
    args = parser.parse_args()

    leds = RgbLeds(args.led_r, args.led_g, args.led_b)

    last_mode = None

    while True:
        ssid = ""
        try:
            ssid = get_connected_ssid()
        except Exception:
            ssid = ""

        if ssid:
            if ssid == args.ssid_blue:
                mode = "blue"
                if mode != last_mode:
                    leds.set_rgb(0, 0, 1)
                    last_mode = mode
                time.sleep(args.poll_sec)
                continue

            if ssid == args.ssid_red:
                mode = "red"
                if mode != last_mode:
                    leds.set_rgb(1, 0, 0)
                    last_mode = mode
                time.sleep(args.poll_sec)
                continue

            mode = "green"
            if mode != last_mode:
                leds.set_rgb(0, 1, 0)
                last_mode = mode
            time.sleep(args.poll_sec)
            continue

        # Not connected: blink R then G then B.
        last_mode = "blink"
        try:
            leds.set_rgb(1, 0, 0)
            time.sleep(args.blink_on_sec)
            leds.off()
            time.sleep(args.blink_off_sec)

            leds.set_rgb(0, 1, 0)
            time.sleep(args.blink_on_sec)
            leds.off()
            time.sleep(args.blink_off_sec)

            leds.set_rgb(0, 0, 1)
            time.sleep(args.blink_on_sec)
            leds.off()
            time.sleep(args.blink_gap_sec)
        except Exception:
            # If LED sysfs is not writable, avoid busy-loop.
            time.sleep(1.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

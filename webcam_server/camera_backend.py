#!/usr/bin/env python3
import argparse
import base64
import collections
import errno
import glob
import importlib.util
import json
import os
import re
import fcntl
import pty
import signal
import shutil
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import numpy as np
except Exception:
    np = None


def _video_dev_sort_key(path: str):
    name = os.path.basename(path)
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 9999


def _load_tiny1b_uvc_module():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(script_dir)
        tool_path = os.path.join(root_dir, "utils", "tiny1b_uvc_cmd.py")
        if not os.path.exists(tool_path):
            return None
        spec = importlib.util.spec_from_file_location("tiny1b_uvc_cmd_runtime", tool_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # dataclasses inspects sys.modules during class decoration.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_TINY1B_UVC_MODULE = _load_tiny1b_uvc_module()


class CameraVtempSampler:
    SAMPLE_INTERVAL_SEC = 1.0

    def __init__(self):
        self._lock = threading.Lock()
        self._tool = _TINY1B_UVC_MODULE
        self._ctrl = None
        self._device = None
        self._last_result = None
        self._last_sample_ts = 0.0

    def _with_age_locked(self, now: float, result: dict):
        out = dict(result)
        if self._last_sample_ts > 0:
            out["sample_age_sec"] = round(max(0.0, now - self._last_sample_ts), 2)
        else:
            out["sample_age_sec"] = None
        return out

    def sample(self, video_device: str):
        now = time.time()
        if not video_device:
            return {
                "ok": False,
                "state": "camera_not_running",
                "raw_u16": None,
                "raw_s16": None,
                "hex": None,
                "sample_age_sec": None,
            }

        with self._lock:
            if self._tool is None:
                return {
                    "ok": False,
                    "state": "tool_unavailable",
                    "error": "tiny1b_uvc_cmd.py not available",
                    "raw_u16": None,
                    "raw_s16": None,
                    "hex": None,
                    "sample_age_sec": None,
                }

            if (
                self._last_result is not None
                and self._device == video_device
                and (now - self._last_sample_ts) < self.SAMPLE_INTERVAL_SEC
            ):
                return self._with_age_locked(now, self._last_result)

            try:
                if self._ctrl is None or self._device != video_device:
                    ep0 = self._tool.resolve_usb_path_from_video(video_device)
                    self._ctrl = self._tool.Tiny1BUvcCtrl(ep0)
                    self._device = video_device

                result = self._ctrl.get_ir_sensor_vtemp()
                self._last_result = {
                    "ok": True,
                    "state": "ok",
                    "device": video_device,
                    "raw_u16": result.get("raw_u16"),
                    "raw_s16": result.get("raw_s16"),
                    "hex": result.get("bytes_hex"),
                }
            except Exception as exc:
                self._ctrl = None
                self._device = None
                self._last_result = {
                    "ok": False,
                    "state": "read_error",
                    "device": video_device,
                    "error": str(exc),
                    "raw_u16": None,
                    "raw_s16": None,
                    "hex": None,
                }

            self._last_sample_ts = now
            return self._with_age_locked(now, self._last_result)


class CameraShutterScheduler:
    def __init__(
        self,
        interval_sec: float = 10.0,
        disable_auto: bool = True,
        min_interval: int = None,
        max_interval: int = None,
    ):
        self._tool = _TINY1B_UVC_MODULE
        self._interval_sec = max(0.0, float(interval_sec))
        self._disable_auto = bool(disable_auto)
        self._min_interval = None if min_interval is None else max(0, min(255, int(min_interval)))
        self._max_interval = None if max_interval is None else max(0, min(255, int(max_interval)))
        self._lock = threading.Lock()
        self._camera_manager = None
        self._ctrl = None
        self._device = None
        self._policy_applied_device = None
        self._policy_applied_signature = None
        self._stop_evt = threading.Event()
        self._thread = None
        self._next_trigger_ts = 0.0
        self._trigger_count = 0
        self._error_count = 0
        self._last_trigger_ts = 0.0
        self._last_ok = None
        self._last_error = ""

    def _reset_ctrl_locked(self):
        self._ctrl = None
        self._device = None
        self._policy_applied_device = None
        self._policy_applied_signature = None

    def _apply_policy_locked(self, video_device: str):
        signature = (self._disable_auto, self._min_interval, self._max_interval)
        if self._policy_applied_device == video_device and self._policy_applied_signature == signature:
            return
        if self._ctrl is None:
            return
        try:
            # auto=1: firmware decides shutter; auto=0: firmware auto shutter disabled.
            self._ctrl.set_shutter_auto_flag(0 if self._disable_auto else 1)
            if self._min_interval is not None:
                self._ctrl.set_shutter_min_interval(self._min_interval)
            if self._max_interval is not None:
                self._ctrl.set_shutter_max_interval(self._max_interval)
            self._policy_applied_device = video_device
            self._policy_applied_signature = signature
            self._last_ok = True
            self._last_error = ""
        except Exception as exc:
            self._error_count += 1
            self._last_ok = False
            self._last_error = f"apply shutter policy failed: {exc}"

    def _ensure_policy_applied_locked(self, video_device: str):
        signature = (self._disable_auto, self._min_interval, self._max_interval)
        if self._policy_applied_device == video_device and self._policy_applied_signature == signature:
            return
        if self._tool is None:
            self._error_count += 1
            self._last_ok = False
            self._last_error = "tiny1b_uvc_cmd module unavailable"
            return
        try:
            if self._ctrl is None or self._device != video_device:
                ep0 = self._tool.resolve_usb_path_from_video(video_device)
                self._ctrl = self._tool.Tiny1BUvcCtrl(ep0)
                self._device = video_device
            self._apply_policy_locked(video_device)
        except Exception as exc:
            self._reset_ctrl_locked()
            self._error_count += 1
            self._last_ok = False
            self._last_error = str(exc)

    def _trigger_manual_shutter_locked(self, video_device: str):
        if self._tool is None:
            self._last_ok = False
            self._last_error = "tiny1b_uvc_cmd module unavailable"
            self._error_count += 1
            return False
        try:
            if self._ctrl is None or self._device != video_device:
                ep0 = self._tool.resolve_usb_path_from_video(video_device)
                self._ctrl = self._tool.Tiny1BUvcCtrl(ep0)
                self._device = video_device
            self._apply_policy_locked(video_device)
            self._ctrl.shutter_manual()
            self._trigger_count += 1
            self._last_trigger_ts = time.time()
            self._last_ok = True
            self._last_error = ""
            return True
        except Exception as exc:
            self._reset_ctrl_locked()
            self._error_count += 1
            self._last_ok = False
            self._last_error = str(exc)
            return False

    def _loop(self):
        # Trigger manual shutter at a fixed cadence while camera is running.
        while not self._stop_evt.wait(0.2):
            if self._camera_manager is None:
                continue

            status = self._camera_manager.get_status()
            running = bool(status.get("running"))
            device = status.get("device")
            now = time.time()

            if not running or not device:
                with self._lock:
                    self._next_trigger_ts = 0.0
                    self._reset_ctrl_locked()
                continue

            with self._lock:
                # Apply auto-shutter policy immediately after camera starts.
                self._ensure_policy_applied_locked(device)
                if self._interval_sec <= 0.0:
                    self._next_trigger_ts = 0.0
                    continue
                if self._next_trigger_ts <= 0.0:
                    self._next_trigger_ts = now + self._interval_sec
                    continue
                if now < self._next_trigger_ts:
                    continue
                self._trigger_manual_shutter_locked(device)
                self._next_trigger_ts = now + self._interval_sec

    def start(self, camera_manager: "CameraManager"):
        self._camera_manager = camera_manager
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    def update_config(self, disable_auto=None, min_interval=None, max_interval=None):
        with self._lock:
            if disable_auto is not None:
                self._disable_auto = bool(disable_auto)
            if min_interval is not None:
                self._min_interval = max(1, min(120, int(min_interval)))
            if max_interval is not None:
                self._max_interval = max(1, min(120, int(max_interval)))

            # Force policy re-apply on next running check.
            self._policy_applied_device = None
            self._policy_applied_signature = None
            self._next_trigger_ts = 0.0

            if self._device and self._ctrl is not None:
                self._apply_policy_locked(self._device)
            return self._status_locked()

    def trigger_once(self):
        if self._camera_manager is None:
            return {
                "ok": False,
                "status_code": 503,
                "error": "camera manager not ready",
                "shutter_scheduler": self.status(),
            }

        cam = self._camera_manager.get_status()
        running = bool(cam.get("running"))
        device = cam.get("device")
        if not running or not device:
            return {
                "ok": False,
                "status_code": 409,
                "error": "camera not running",
                "shutter_scheduler": self.status(),
            }

        with self._lock:
            ok = self._trigger_manual_shutter_locked(device)
            snap = self._status_locked()
            err = self._last_error
        if not ok:
            return {
                "ok": False,
                "status_code": 500,
                "error": err or "manual shutter trigger failed",
                "shutter_scheduler": snap,
            }
        return {"ok": True, "shutter_scheduler": snap}

    def status(self):
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        now = time.time()
        next_in = None
        if self._next_trigger_ts > 0.0:
            next_in = round(max(0.0, self._next_trigger_ts - now), 2)
        age = None
        if self._last_trigger_ts > 0.0:
            age = round(max(0.0, now - self._last_trigger_ts), 2)
        return {
            "enabled": self._interval_sec > 0.0,
            "interval_sec": self._interval_sec,
            "disable_auto": self._disable_auto,
            "min_interval": self._min_interval,
            "max_interval": self._max_interval,
            "next_trigger_in_sec": next_in,
            "trigger_count": self._trigger_count,
            "error_count": self._error_count,
            "last_trigger_age_sec": age,
            "last_ok": self._last_ok,
            "last_error": self._last_error,
        }


class SystemMetricsSampler:
    def __init__(self, camera_vtemp_sampler: CameraVtempSampler = None):
        self._lock = threading.Lock()
        self._prev_total = None
        self._prev_idle = None
        self._last_cpu_percent = 0.0
        self._camera_vtemp_sampler = camera_vtemp_sampler

    @staticmethod
    def _read_cpu_counters():
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
        parts = line.strip().split()
        if not parts or parts[0] != "cpu":
            return None, None
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return total, idle

    def sample_cpu_percent(self):
        total, idle = self._read_cpu_counters()
        if total is None:
            return self._last_cpu_percent

        with self._lock:
            if self._prev_total is None or self._prev_idle is None:
                self._prev_total = total
                self._prev_idle = idle
                return round(self._last_cpu_percent, 1)

            delta_total = total - self._prev_total
            delta_idle = idle - self._prev_idle
            self._prev_total = total
            self._prev_idle = idle

            if delta_total > 0:
                self._last_cpu_percent = max(
                    0.0, min(100.0, (1.0 - (delta_idle / float(delta_total))) * 100.0)
                )
            return round(self._last_cpu_percent, 1)

    @staticmethod
    def _read_meminfo():
        info = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        return info

    def sample_memory(self):
        try:
            mem = self._read_meminfo()
            total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
            avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
            used_kb = max(0, total_kb - avail_kb)
            percent = (used_kb / total_kb * 100.0) if total_kb > 0 else 0.0
            return {
                "percent": round(percent, 1),
                "used_mb": round(used_kb / 1024.0, 1),
                "total_mb": round(total_kb / 1024.0, 1),
            }
        except Exception:
            return {"percent": None, "used_mb": None, "total_mb": None}

    @staticmethod
    def sample_load():
        try:
            l1, l5, l15 = os.getloadavg()
            return [round(l1, 2), round(l5, 2), round(l15, 2)]
        except Exception:
            return [None, None, None]

    @staticmethod
    def sample_disk():
        try:
            du = shutil.disk_usage("/")
            used = du.total - du.free
            return {
                "percent": round((used / du.total) * 100.0, 1) if du.total > 0 else 0.0,
                "used_gb": round(used / (1024.0 ** 3), 2),
                "total_gb": round(du.total / (1024.0 ** 3), 2),
            }
        except Exception:
            return {"percent": None, "used_gb": None, "total_gb": None}

    @staticmethod
    def sample_temperature_c():
        try:
            zones = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
            for path in zones:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read().strip()
                    if not raw:
                        continue
                    v = float(raw)
                    if v > 1000.0:
                        v = v / 1000.0
                    if 0.0 < v < 150.0:
                        return round(v, 1)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def sample_uptime_sec():
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                return int(float(f.read().split()[0]))
        except Exception:
            return None

    def snapshot(self, camera_status: dict):
        video_device = None
        if camera_status and camera_status.get("running"):
            video_device = camera_status.get("device")

        camera_vtemp = None
        if self._camera_vtemp_sampler is not None:
            camera_vtemp = self._camera_vtemp_sampler.sample(video_device)

        return {
            "cpu_percent": self.sample_cpu_percent(),
            "memory": self.sample_memory(),
            "load_avg": self.sample_load(),
            "disk": self.sample_disk(),
            "temperature_c": self.sample_temperature_c(),
            "uptime_sec": self.sample_uptime_sec(),
            "camera": camera_status,
            "camera_vtemp": camera_vtemp,
            "ts": int(time.time()),
        }


class CameraManager:
    # Match yombir's camera format assumptions.
    CAPTURE_WIDTH = 256
    CAPTURE_HEIGHT = 384
    CAPTURE_FPS = 25
    THERMAL_WIDTH = 256
    THERMAL_HEIGHT = 192
    # Keep output fps aligned with capture fps to prevent encoder queue buildup
    # (which causes multi-second stale frames / high latency).
    OUTPUT_FPS = 25

    CAPTURE_FRAME_BYTES = CAPTURE_WIDTH * CAPTURE_HEIGHT * 2  # yuyv422 = 2 bytes per pixel
    THERMAL_PLANE_BYTES = THERMAL_WIDTH * THERMAL_HEIGHT * 2   # gray16le plane bytes
    LUT_SIZE = 65536

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        self._capture_proc = None
        self._encoder_proc = None

        self._decode_thread = None
        self._reader_thread = None
        self._capture_stderr_thread = None
        self._encoder_stderr_thread = None

        self._running = False
        self._device = None
        self._latest_frame = None
        self._latest_raw16 = None
        self._frame_id = 0
        self._jpeg_frame_id = 0
        self._viewer_count = 0
        self._last_client_activity_ts = 0.0
        self._last_frame_ts = 0.0
        self._last_error = ""
        self._desired_running = False
        self._restart_backoff_until = 0.0
        self._restart_count = 0
        # Client does pseudo-color rendering; backend only sends raw gray16 frames.
        self._client_colorize = True

        self._gradient = self._load_gradient()
        self._palettes = self._build_palettes(self._gradient)
        self._palette_name = "iron" if "iron" in self._palettes else next(iter(self._palettes.keys()))
        self._gradient = self._palettes[self._palette_name]
        self._gradient_np = None
        if np is not None:
            # gradient.bin is RGB triplets indexed by 16-bit thermal value.
            self._gradient_np = np.frombuffer(self._gradient, dtype=np.uint8).reshape(self.LUT_SIZE, 3)

        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self):
        # Auto-stop when webpage is no longer actively pulling frames.
        # This satisfies: open only on button click, close automatically when page disconnects.
        idle_timeout_sec = 4.0
        stale_frame_timeout_sec = 2.0
        while True:
            time.sleep(0.5)
            with self._cond:
                now = time.time()

                idle = None
                if self._last_client_activity_ts > 0:
                    idle = now - self._last_client_activity_ts

                # Close when page is gone/inactive.
                if idle is not None and idle > idle_timeout_sec:
                    if self._running or self._desired_running:
                        self._desired_running = False
                        self._last_error = "auto-stopped: webpage disconnected or inactive"
                        self._stop_locked()
                        self._cond.notify_all()
                    continue

                # Only self-heal when user has requested camera open and page is still active.
                if not self._desired_running:
                    continue
                if self._last_client_activity_ts <= 0:
                    continue

                # Stream process exited or errored: restart with short backoff.
                if not self._running:
                    if now >= self._restart_backoff_until:
                        self._restart_backoff_until = now + 1.0
                        self._restart_pipeline_locked("pipeline stopped unexpectedly")
                    continue

                # Stream is running but stale (no new frame for too long): restart.
                if self._last_frame_ts > 0 and (now - self._last_frame_ts) > stale_frame_timeout_sec:
                    if now >= self._restart_backoff_until:
                        self._restart_backoff_until = now + 1.0
                        self._restart_pipeline_locked("stale frame detected")
                    continue

    def touch_client_activity(self):
        with self._lock:
            self._last_client_activity_ts = time.time()

    def _pipeline_running_locked(self):
        if not self._running:
            return False
        if self._capture_proc is None or self._capture_proc.poll() is not None:
            return False
        if self._encoder_proc is not None and self._encoder_proc.poll() is not None:
            return False
        return True

    def _build_capture_cmd(self, device: str):
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-thread_queue_size",
            "2",
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-f",
            "v4l2",
            "-input_format",
            "yuyv422",
            "-video_size",
            f"{self.CAPTURE_WIDTH}x{self.CAPTURE_HEIGHT}",
            "-framerate",
            str(self.CAPTURE_FPS),
            "-i",
            device,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuyv422",
            "-",
        ]

    def _build_encode_cmd(self):
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{self.THERMAL_WIDTH}x{self.THERMAL_HEIGHT}",
            "-r",
            str(self.OUTPUT_FPS),
            "-i",
            "-",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "10",
            "-",
        ]

    def _start_pipeline_locked(self, device: str):
        capture_cmd = self._build_capture_cmd(device)
        encode_cmd = None if self._client_colorize else self._build_encode_cmd()

        capture_proc = None
        encoder_proc = None
        try:
            capture_proc = subprocess.Popen(
                capture_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
            if encode_cmd is not None:
                encoder_proc = subprocess.Popen(
                    encode_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=True,
                )
        except Exception as exc:
            try:
                if capture_proc and capture_proc.poll() is None:
                    capture_proc.terminate()
            except Exception:
                pass
            return False, f"无法启动 ffmpeg 管线: {exc}"

        self._capture_proc = capture_proc
        self._encoder_proc = encoder_proc
        self._running = True
        self._device = device
        self._latest_frame = None
        self._latest_raw16 = None
        self._frame_id = 0
        self._jpeg_frame_id = 0
        self._last_error = ""
        self._viewer_count = 0
        self._last_client_activity_ts = time.time()
        self._last_frame_ts = 0.0

        self._decode_thread = threading.Thread(target=self._decode_loop, daemon=True)
        self._capture_stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(capture_proc, "capture"),
            daemon=True,
        )

        self._decode_thread.start()
        self._capture_stderr_thread.start()
        if encoder_proc is not None:
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._encoder_stderr_thread = threading.Thread(
                target=self._stderr_loop,
                args=(encoder_proc, "encode"),
                daemon=True,
            )
            self._reader_thread.start()
            self._encoder_stderr_thread.start()
        else:
            self._reader_thread = None
            self._encoder_stderr_thread = None

        deadline = time.time() + 8.0
        while self._running and self._latest_raw16 is None and time.time() < deadline:
            self._cond.wait(timeout=0.2)

        if self._latest_raw16 is None:
            err = self._last_error or "打开摄像头失败，未收到热成像视频帧"
            self._stop_locked()
            self._cond.notify_all()
            return False, err

        return True, ""

    def _restart_pipeline_locked(self, reason: str):
        device = self._device
        if not device:
            self._desired_running = False
            self._last_error = f"auto-recover failed: missing device ({reason})"
            self._stop_locked()
            self._cond.notify_all()
            return

        self._last_error = f"auto-recover: restarting pipeline ({reason})"
        self._stop_locked()
        ok, err = self._start_pipeline_locked(device)
        if ok:
            self._restart_count += 1
            self._last_error = f"auto-recover: restarted successfully (count={self._restart_count})"
        else:
            self._last_error = f"auto-recover failed: {err}"
        self._cond.notify_all()

    def _load_gradient(self) -> bytes:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(script_dir)
        gradient_path = os.path.join(root_dir, "gradients", "gradient.bin")
        try:
            with open(gradient_path, "rb") as f:
                data = f.read()
        except Exception as exc:
            raise RuntimeError(f"无法读取 yombir 渐变文件: {gradient_path} ({exc})")

        if len(data) != 65536 * 3:
            raise RuntimeError(
                f"yombir 渐变文件长度不正确: {gradient_path}, got={len(data)}, expected={65536*3}"
            )
        return data

    @staticmethod
    def _clamp_u8(v: float) -> int:
        if v < 0:
            return 0
        if v > 255:
            return 255
        return int(v + 0.5)

    def _build_gradient_from_stops(self, stops):
        if not stops:
            return bytes([0, 0, 0] * self.LUT_SIZE)
        stops = sorted(stops, key=lambda x: x[0])
        if stops[0][0] > 0.0:
            stops = [(0.0, stops[0][1])] + stops
        if stops[-1][0] < 1.0:
            stops = stops + [(1.0, stops[-1][1])]

        out = bytearray(self.LUT_SIZE * 3)
        seg = 0
        for i in range(self.LUT_SIZE):
            p = i / float(self.LUT_SIZE - 1)
            while seg + 1 < len(stops) and p > stops[seg + 1][0]:
                seg += 1

            p0, c0 = stops[seg]
            p1, c1 = stops[min(seg + 1, len(stops) - 1)]
            if p1 <= p0:
                t = 0.0
            else:
                t = (p - p0) / (p1 - p0)

            r = self._clamp_u8(c0[0] + (c1[0] - c0[0]) * t)
            g = self._clamp_u8(c0[1] + (c1[1] - c0[1]) * t)
            b = self._clamp_u8(c0[2] + (c1[2] - c0[2]) * t)

            j = i * 3
            out[j + 0] = r
            out[j + 1] = g
            out[j + 2] = b

        return bytes(out)

    def _build_palettes(self, default_gradient: bytes):
        palettes = {
            "yombir": default_gradient,
            "iron": self._build_gradient_from_stops(
                [
                    (0.00, (0, 0, 0)),
                    (0.15, (30, 0, 60)),
                    (0.35, (120, 0, 20)),
                    (0.60, (220, 70, 0)),
                    (0.80, (255, 190, 20)),
                    (1.00, (255, 255, 255)),
                ]
            ),
            "rainbow": self._build_gradient_from_stops(
                [
                    (0.00, (0, 0, 40)),
                    (0.18, (0, 50, 220)),
                    (0.40, (0, 220, 255)),
                    (0.62, (0, 230, 0)),
                    (0.80, (255, 230, 0)),
                    (0.92, (255, 90, 0)),
                    (1.00, (255, 255, 255)),
                ]
            ),
            "white_hot": self._build_gradient_from_stops(
                [
                    (0.00, (0, 0, 0)),
                    (1.00, (255, 255, 255)),
                ]
            ),
            "black_hot": self._build_gradient_from_stops(
                [
                    (0.00, (255, 255, 255)),
                    (1.00, (0, 0, 0)),
                ]
            ),
            "arctic": self._build_gradient_from_stops(
                [
                    (0.00, (0, 0, 0)),
                    (0.25, (0, 30, 120)),
                    (0.55, (0, 130, 255)),
                    (0.80, (120, 220, 255)),
                    (1.00, (255, 255, 255)),
                ]
            ),
            "lava": self._build_gradient_from_stops(
                [
                    (0.00, (0, 0, 0)),
                    (0.25, (80, 0, 0)),
                    (0.50, (180, 10, 0)),
                    (0.75, (255, 90, 0)),
                    (0.92, (255, 180, 40)),
                    (1.00, (255, 250, 210)),
                ]
            ),
        }
        return palettes

    def set_palette(self, name: str):
        if not name:
            return False, "palette name is empty"

        with self._lock:
            lut = self._palettes.get(name)
            if lut is None:
                return False, f"unsupported palette: {name}"
            self._palette_name = name
            self._gradient = lut
            if np is not None:
                self._gradient_np = np.frombuffer(self._gradient, dtype=np.uint8).reshape(self.LUT_SIZE, 3)
            else:
                self._gradient_np = None
        return True, ""

    def get_palette_info(self):
        with self._lock:
            return {
                "current": self._palette_name,
                "available": sorted(self._palettes.keys()),
            }

    def _list_video_devices(self):
        devices = []
        for path in sorted(glob.glob("/dev/video*"), key=_video_dev_sort_key):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if stat.S_ISCHR(st.st_mode):
                devices.append(path)
        return devices

    def _is_capture_device(self, path: str) -> bool:
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "-D", "-d", path],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=2,
            )
        except Exception:
            return False

        if "Device Caps" not in out:
            return False
        dev_caps = out.split("Device Caps", 1)[1]
        return re.search(r"^\s+Video Capture\s*$", dev_caps, re.MULTILINE) is not None

    def find_camera(self):
        candidates = []
        for dev in self._list_video_devices():
            if self._is_capture_device(dev):
                candidates.append(dev)
        if not candidates:
            return None, []
        return candidates[0], candidates

    def _set_runtime_error(self, message: str):
        with self._cond:
            if self._running:
                self._running = False
            if message:
                self._last_error = message
                # Kick watchdog to restart quickly if page is still active.
                self._restart_backoff_until = 0.0
            self._cond.notify_all()

    @staticmethod
    def _read_exact(stream, size: int):
        if stream is None:
            return None
        buf = bytearray()
        while len(buf) < size:
            chunk = stream.read(size - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _stderr_loop(self, proc, label: str):
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="ignore").strip()
            if line:
                with self._lock:
                    self._last_error = f"{label}: {line}"

    def _thermal_plane_to_bgr(self, plane: bytes) -> bytes:
        if self._gradient_np is not None and np is not None:
            return self._thermal_plane_to_bgr_numpy(plane)

        min_value = 65535
        max_value = 0

        # 16-bit little-endian min/max scan
        for i in range(0, len(plane), 2):
            v = plane[i] | (plane[i + 1] << 8)
            if v < min_value:
                min_value = v
            if v > max_value:
                max_value = v

        value_range = max_value - min_value
        out = bytearray(self.THERMAL_WIDTH * self.THERMAL_HEIGHT * 3)
        gradient = self._gradient

        o = 0
        if value_range <= 0:
            # Keep frame valid even if sensor reports a flat plane.
            p = min_value * 3
            b = gradient[p + 2]
            g = gradient[p + 1]
            r = gradient[p + 0]
            for _ in range(self.THERMAL_WIDTH * self.THERMAL_HEIGHT):
                out[o] = b
                out[o + 1] = g
                out[o + 2] = r
                o += 3
            return bytes(out)

        pixel_count = self.THERMAL_WIDTH * self.THERMAL_HEIGHT
        for idx in range(pixel_count):
            # Match yombir default orientation (rotate180 = true).
            i = (pixel_count - 1 - idx) * 2
            v = plane[i] | (plane[i + 1] << 8)
            normalized = ((v - min_value) * 65535) // value_range
            p = normalized * 3
            # yombir stores to OpenCV BGR.
            out[o] = gradient[p + 2]
            out[o + 1] = gradient[p + 1]
            out[o + 2] = gradient[p + 0]
            o += 3

        return bytes(out)

    def _thermal_plane_to_bgr_numpy(self, plane: bytes) -> bytes:
        pixel_count = self.THERMAL_WIDTH * self.THERMAL_HEIGHT
        arr = np.frombuffer(plane, dtype="<u2", count=pixel_count)
        if arr.size != pixel_count:
            return b""

        # Match yombir default orientation (rotate180 = true).
        arr = arr[::-1]

        min_value = int(arr.min())
        max_value = int(arr.max())
        if max_value <= min_value:
            normalized = np.full(arr.shape, min_value, dtype=np.uint16)
        else:
            arr32 = arr.astype(np.uint32)
            normalized = (((arr32 - min_value) * 65535) // (max_value - min_value)).astype(np.uint16)

        # gradient is RGB; yombir outputs BGR for OpenCV.
        rgb = self._gradient_np[normalized]
        bgr = rgb[:, ::-1]
        bgr = np.ascontiguousarray(bgr.reshape(self.THERMAL_HEIGHT, self.THERMAL_WIDTH, 3))
        return bgr.tobytes()

    def _decode_loop(self):
        capture = self._capture_proc
        encoder = self._encoder_proc
        if capture is None or capture.stdout is None:
            self._set_runtime_error("stream pipeline unavailable")
            return

        half = self.THERMAL_PLANE_BYTES

        try:
            while True:
                raw_frame = self._read_exact(capture.stdout, self.CAPTURE_FRAME_BYTES)
                if raw_frame is None:
                    break

                # Match yombir: use the second half plane as thermal gray16 data.
                thermal_plane = raw_frame[half : half + self.THERMAL_PLANE_BYTES]
                with self._cond:
                    self._latest_raw16 = thermal_plane
                    self._frame_id += 1
                    self._last_frame_ts = time.time()
                    self._cond.notify_all()

                # Optional backend JPEG pipeline, disabled by default in client-colorize mode.
                if encoder is not None and encoder.stdin is not None:
                    bgr = self._thermal_plane_to_bgr(thermal_plane)
                    try:
                        encoder.stdin.write(bgr)
                        encoder.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        break
        finally:
            try:
                if encoder is not None and encoder.stdin:
                    encoder.stdin.close()
            except Exception:
                pass

            if self._running:
                code = capture.poll()
                self._set_runtime_error(f"capture stream ended (ffmpeg exit code: {code})")

    def _reader_loop(self):
        buf = bytearray()
        proc = self._encoder_proc
        if proc is None or proc.stdout is None:
            self._set_runtime_error("encoder stdout unavailable")
            return

        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi < 0:
                        if len(buf) > 2:
                            del buf[:-2]
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi < 0:
                        if soi > 0:
                            del buf[:soi]
                        break

                    frame = bytes(buf[soi : eoi + 2])
                    del buf[: eoi + 2]
                    with self._cond:
                        self._latest_frame = frame
                        self._jpeg_frame_id += 1
                        self._cond.notify_all()
        finally:
            if self._running:
                code = proc.poll()
                self._set_runtime_error(f"jpeg stream ended (ffmpeg exit code: {code})")

    def _stop_locked(self):
        capture = self._capture_proc
        encoder = self._encoder_proc

        self._running = False
        self._capture_proc = None
        self._encoder_proc = None
        self._device = None
        self._latest_frame = None
        self._latest_raw16 = None
        self._viewer_count = 0
        self._last_client_activity_ts = 0.0
        self._last_frame_ts = 0.0

        if encoder is not None:
            try:
                if encoder.stdin:
                    encoder.stdin.close()
            except Exception:
                pass

        for proc in (capture, encoder):
            if proc is None:
                continue
            if proc.poll() is not None:
                continue

            try:
                pgid = os.getpgid(proc.pid)
            except Exception:
                pgid = None

            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                pass

            if proc.poll() is None:
                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=1.5)
                except Exception:
                    pass

    def stop(self):
        with self._cond:
            self._desired_running = False
            self._stop_locked()
            self._cond.notify_all()

    def add_viewer(self) -> bool:
        with self._lock:
            running = self._pipeline_running_locked()
            if not running:
                return False
            self._viewer_count += 1
            return True

    def remove_viewer(self):
        with self._cond:
            if self._viewer_count > 0:
                self._viewer_count -= 1

            # Auto-close camera when the last webpage stream disconnects.
            if self._viewer_count == 0 and self._running:
                self._last_error = "auto-stopped: no active webpage viewers"
                self._stop_locked()
            self._cond.notify_all()

    def open_camera(self):
        with self._cond:
            self._desired_running = True
            if self._pipeline_running_locked():
                return True, {"device": self._device, "message": "already running"}

        device, candidates = self.find_camera()
        if not device:
            with self._lock:
                self._desired_running = False
            return (
                False,
                {
                    "error": "未找到可用摄像头（Video Capture）",
                    "candidates": candidates,
                },
            )

        with self._cond:
            ok, err = self._start_pipeline_locked(device)
            if not ok:
                self._desired_running = False
                return False, {"error": err, "device": device}
            self._desired_running = True

        return True, {"device": device, "message": "camera opened with yombir decode"}

    def get_status(self):
        with self._lock:
            running = self._pipeline_running_locked()
            return {
                "running": running,
                "device": self._device,
                "has_frame": running and (self._latest_raw16 is not None),
                "viewers": self._viewer_count,
                "desired_running": self._desired_running,
                "palette": self._palette_name,
                "frame_age_sec": (
                    round(time.time() - self._last_frame_ts, 2)
                    if running and self._last_frame_ts > 0
                    else None
                ),
                "last_client_activity_age_sec": (
                    round(time.time() - self._last_client_activity_ts, 2)
                    if self._last_client_activity_ts > 0
                    else None
                ),
                "last_error": self._last_error,
            }

    def wait_for_next_raw_frame(self, prev_id: int, timeout: float = 5.0):
        with self._cond:
            end = time.time() + timeout
            while self._frame_id <= prev_id and self._running:
                remain = end - time.time()
                if remain <= 0:
                    break
                self._cond.wait(timeout=remain)

            return self._latest_raw16, self._frame_id, self._running

    def wait_for_next_jpeg_frame(self, prev_id: int, timeout: float = 5.0):
        with self._cond:
            end = time.time() + timeout
            while self._jpeg_frame_id <= prev_id and self._running:
                remain = end - time.time()
                if remain <= 0:
                    break
                self._cond.wait(timeout=remain)

            return self._latest_frame, self._jpeg_frame_id, self._running


class TerminalManager:
    MAX_OUTPUT_CHARS = 60000
    DEFAULT_TIMEOUT_SEC = 20.0

    def __init__(self, initial_cwd: str = None):
        self._lock = threading.Lock()
        self._cwd = initial_cwd or os.getcwd()

    @staticmethod
    def _clip_output(text: str, limit: int):
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        remain = len(text) - limit
        return text[:limit] + f"\n\n[output truncated, omitted {remain} chars]"

    @staticmethod
    def _parse_positive_timeout(raw):
        try:
            timeout = float(raw)
        except Exception:
            return TerminalManager.DEFAULT_TIMEOUT_SEC
        if timeout <= 0:
            return TerminalManager.DEFAULT_TIMEOUT_SEC
        return min(timeout, 60.0)

    def _resolve_dir(self, target: str):
        base = os.path.expanduser(target) if target else os.path.expanduser("~")
        if not os.path.isabs(base):
            base = os.path.join(self._cwd, base)
        resolved = os.path.abspath(base)
        if not os.path.isdir(resolved):
            return None, f"cd: no such directory: {target if target else '~'}"
        return resolved, ""

    def execute(self, command: str, timeout_sec: float = None):
        cmd = (command or "").strip()
        if not cmd:
            return {
                "ok": True,
                "code": 0,
                "cwd": self._cwd,
                "output": "",
                "message": "empty command",
            }

        with self._lock:
            if cmd in ("clear", "cls"):
                return {"ok": True, "code": 0, "cwd": self._cwd, "output": "", "clear": True}

            if cmd == "cd" or cmd.startswith("cd "):
                target = cmd[2:].strip()
                new_cwd, err = self._resolve_dir(target)
                if new_cwd is None:
                    return {"ok": False, "code": 1, "cwd": self._cwd, "output": err}
                self._cwd = new_cwd
                return {"ok": True, "code": 0, "cwd": self._cwd, "output": ""}

            marker = f"__YOMBIR_CWD_{int(time.time() * 1000)}_{os.getpid()}__"
            wrapped = f"{cmd}\nprintf '\\n{marker}%s\\n' \"$PWD\""
            timeout = timeout_sec or self.DEFAULT_TIMEOUT_SEC

            try:
                proc = subprocess.run(
                    ["bash", "-lc", wrapped],
                    cwd=self._cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    env=os.environ.copy(),
                )
                combined = proc.stdout or ""
                lines = combined.splitlines()
                next_cwd = None
                clean_lines = []
                for line in lines:
                    if line.startswith(marker):
                        next_cwd = line[len(marker) :].strip()
                        continue
                    clean_lines.append(line)
                output = "\n".join(clean_lines)
                output = self._clip_output(output, self.MAX_OUTPUT_CHARS)
                if next_cwd and os.path.isdir(next_cwd):
                    self._cwd = next_cwd
                return {
                    "ok": proc.returncode == 0,
                    "code": int(proc.returncode),
                    "cwd": self._cwd,
                    "output": output,
                }
            except subprocess.TimeoutExpired as exc:
                out = ""
                if exc.stdout:
                    if isinstance(exc.stdout, bytes):
                        out = exc.stdout.decode("utf-8", errors="replace")
                    else:
                        out = str(exc.stdout)
                clipped = self._clip_output(out, self.MAX_OUTPUT_CHARS)
                extra = "\n" if clipped else ""
                return {
                    "ok": False,
                    "code": 124,
                    "cwd": self._cwd,
                    "output": f"{clipped}{extra}command timed out after {timeout:.1f}s",
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "code": 500,
                    "cwd": self._cwd,
                    "output": f"terminal execution error: {exc}",
                }


class PtyTerminalManager:
    MAX_BUFFER_BYTES = 1_000_000
    DEFAULT_TIMEOUT_MS = 25000

    def __init__(self, shell: str = "/bin/bash"):
        self._shell = shell
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._proc = None
        self._master_fd = None
        self._reader_thread = None
        self._chunks = collections.deque()  # (seq, bytes)
        self._bytes_total = 0
        self._seq = 0
        self._dropped_until_seq = 0
        self._exit_code = None
        self._starting = False

    @staticmethod
    def _safe_cols_rows(cols, rows):
        try:
            cols_i = int(cols)
        except Exception:
            cols_i = 120
        try:
            rows_i = int(rows)
        except Exception:
            rows_i = 34
        cols_i = max(40, min(400, cols_i))
        rows_i = max(10, min(200, rows_i))
        return cols_i, rows_i

    @staticmethod
    def _parse_positive_timeout_ms(raw):
        try:
            timeout = int(raw)
        except Exception:
            return PtyTerminalManager.DEFAULT_TIMEOUT_MS
        if timeout <= 0:
            return PtyTerminalManager.DEFAULT_TIMEOUT_MS
        return max(100, min(60000, timeout))

    def _is_running_locked(self):
        return self._proc is not None and self._proc.poll() is None and self._master_fd is not None

    def _append_chunk_locked(self, data: bytes):
        if not data:
            return
        self._seq += 1
        self._chunks.append((self._seq, data))
        self._bytes_total += len(data)

        while self._bytes_total > self.MAX_BUFFER_BYTES and self._chunks:
            old_seq, old_data = self._chunks.popleft()
            self._bytes_total -= len(old_data)
            self._dropped_until_seq = max(self._dropped_until_seq, old_seq)

        self._cv.notify_all()

    def _set_winsize_locked(self, cols: int, rows: int):
        if self._master_fd is None:
            return
        try:
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed)
        except Exception:
            return

    def _reader_loop(self):
        while True:
            with self._lock:
                master_fd = self._master_fd
                proc = self._proc
            if master_fd is None:
                break

            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                # EIO indicates the pty slave side is closed (shell exited).
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                time.sleep(0.02)
                continue

            if not data:
                break

            with self._lock:
                self._append_chunk_locked(data)
                if proc is not None and proc.poll() is not None:
                    break

        with self._lock:
            code = None
            if self._proc is not None:
                code = self._proc.poll()
                if code is None:
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
                    try:
                        self._proc.wait(timeout=0.5)
                    except Exception:
                        pass
                    code = self._proc.poll()

            self._exit_code = code
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except Exception:
                    pass
                self._master_fd = None
            self._proc = None
            self._reader_thread = None
            self._cv.notify_all()

    def _start_locked(self, cols: int, rows: int):
        if self._starting:
            return
        self._starting = True
        try:
            master_fd, slave_fd = pty.openpty()
            packed = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, packed)

            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")

            proc = subprocess.Popen(
                [self._shell, "-il"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                cwd=os.getcwd(),
                env=env,
            )
            os.close(slave_fd)

            self._proc = proc
            self._master_fd = master_fd
            self._exit_code = None
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
        finally:
            self._starting = False

    def start(self, cols=120, rows=34):
        cols_i, rows_i = self._safe_cols_rows(cols, rows)
        with self._lock:
            if self._is_running_locked():
                self._set_winsize_locked(cols_i, rows_i)
            else:
                self._start_locked(cols_i, rows_i)
            return {
                "ok": True,
                "running": self._is_running_locked(),
                "seq": self._seq,
                "exit_code": self._exit_code,
            }

    def stop(self):
        with self._lock:
            proc = self._proc
            master_fd = self._master_fd
            self._proc = None
            self._master_fd = None
            self._reader_thread = None
            self._exit_code = None

            if master_fd is not None:
                try:
                    os.close(master_fd)
                except Exception:
                    pass

            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=1.2)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            self._cv.notify_all()
            return {"ok": True}

    def resize(self, cols, rows):
        cols_i, rows_i = self._safe_cols_rows(cols, rows)
        with self._lock:
            self._set_winsize_locked(cols_i, rows_i)
            return {
                "ok": True,
                "running": self._is_running_locked(),
                "cols": cols_i,
                "rows": rows_i,
            }

    def write(self, data: str):
        if data is None:
            data = ""
        with self._lock:
            if not self._is_running_locked():
                return {"ok": False, "error": "terminal session not running"}
            master_fd = self._master_fd

        try:
            payload = data.encode("utf-8", errors="replace")
            if payload:
                os.write(master_fd, payload)
            return {"ok": True, "written": len(payload)}
        except Exception as exc:
            return {"ok": False, "error": f"write failed: {exc}"}

    def read(self, after_seq: int, timeout_ms: int):
        timeout_ms_i = self._parse_positive_timeout_ms(timeout_ms)
        try:
            after = int(after_seq)
        except Exception:
            after = 0

        deadline = time.time() + (timeout_ms_i / 1000.0)
        with self._cv:
            while self._seq <= after and self._is_running_locked():
                remain = deadline - time.time()
                if remain <= 0:
                    break
                self._cv.wait(timeout=remain)

            dropped = after < self._dropped_until_seq
            out_parts = []
            out_bytes = 0
            next_seq = after
            for seq, data in self._chunks:
                if seq <= after:
                    continue
                if out_bytes + len(data) > 256000 and out_parts:
                    break
                out_parts.append(data)
                out_bytes += len(data)
                next_seq = seq
                if len(out_parts) >= 128:
                    break

            merged = b"".join(out_parts)
            payload_b64 = base64.b64encode(merged).decode("ascii") if merged else ""
            return {
                "ok": True,
                "running": self._is_running_locked(),
                "seq": next_seq,
                "dropped": dropped,
                "exit_code": self._exit_code,
                "data_b64": payload_b64,
            }


class CameraHandler(BaseHTTPRequestHandler):
    manager: CameraManager = None
    metrics: SystemMetricsSampler = None
    terminal: TerminalManager = None
    pty_terminal: PtyTerminalManager = None
    shutter_scheduler: CameraShutterScheduler = None

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/status":
            self._json(200, self.manager.get_status())
            return

        if path == "/api/palette":
            self._json(200, self.manager.get_palette_info())
            return

        if path == "/api/metrics":
            camera_status = self.manager.get_status()
            payload = self.metrics.snapshot(camera_status)
            if self.shutter_scheduler is not None:
                payload["shutter_scheduler"] = self.shutter_scheduler.status()
            self._json(200, payload)
            return

        if path == "/api/shutter/config":
            if self.shutter_scheduler is None:
                self._json(503, {"error": "shutter scheduler not ready"})
                return
            self._json(200, {"ok": True, "shutter_scheduler": self.shutter_scheduler.status()})
            return

        if path == "/api/terminal/session/read":
            if self.pty_terminal is None:
                self._json(503, {"error": "pty terminal not ready"})
                return
            query = parse_qs(parsed.query or "", keep_blank_values=True)
            after = query.get("after", ["0"])[0]
            timeout_ms = query.get("timeout_ms", [str(PtyTerminalManager.DEFAULT_TIMEOUT_MS)])[0]
            result = self.pty_terminal.read(after, timeout_ms)
            self._json(200, result)
            return

        if path == "/frame.raw":
            self.manager.touch_client_activity()
            status = self.manager.get_status()
            if not status["running"]:
                self._json(503, {"error": "camera not running", **status})
                return

            frame, _, running = self.manager.wait_for_next_raw_frame(-1, timeout=2.0)
            if not running or frame is None:
                self._json(503, {"error": "no raw frame available", **self.manager.get_status()})
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Frame-Width", str(self.manager.THERMAL_WIDTH))
            self.send_header("X-Frame-Height", str(self.manager.THERMAL_HEIGHT))
            self.send_header("X-Frame-Format", "gray16le")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return

        if path == "/frame.jpg":
            # JPEG route is kept for compatibility. In client-colorize mode, backend
            # intentionally skips JPEG encoding to reduce CPU load.
            if self.manager._encoder_proc is None:
                self._json(410, {"error": "jpeg route disabled in raw mode; use /frame.raw"})
                return

            self.manager.touch_client_activity()
            status = self.manager.get_status()
            if not status["running"]:
                self._json(503, {"error": "camera not running", **status})
                return

            frame, _, running = self.manager.wait_for_next_jpeg_frame(-1, timeout=2.0)
            if not running or frame is None:
                self._json(503, {"error": "no frame available", **self.manager.get_status()})
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return

        if path == "/stream.mjpg":
            if self.manager._encoder_proc is None:
                self._json(410, {"error": "mjpeg route disabled in raw mode; use /frame.raw"})
                return
            if not self.manager.add_viewer():
                status = self.manager.get_status()
                self._json(503, {"error": "camera not running", **status})
                return

            self.manager.touch_client_activity()

            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            last_id = -1
            try:
                while True:
                    frame, frame_id, running = self.manager.wait_for_next_jpeg_frame(last_id, timeout=5.0)
                    if frame is None:
                        if not running:
                            break
                        continue
                    if frame_id == last_id:
                        continue
                    last_id = frame_id

                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    )
                    self.wfile.write(header)
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    self.manager.touch_client_activity()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.manager.remove_viewer()
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/shutter/trigger":
            if self.shutter_scheduler is None:
                self._json(503, {"error": "shutter scheduler not ready"})
                return
            result = self.shutter_scheduler.trigger_once()
            if result.get("ok"):
                self._json(200, result)
                return
            code = int(result.get("status_code") or 500)
            self._json(
                code,
                {
                    "error": result.get("error") or "manual shutter trigger failed",
                    "shutter_scheduler": result.get("shutter_scheduler"),
                },
            )
            return

        if path == "/api/shutter/config":
            if self.shutter_scheduler is None:
                self._json(503, {"error": "shutter scheduler not ready"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                self._json(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "invalid payload"})
                return

            disable_auto = None
            mode = payload.get("mode")
            if mode is not None:
                mode_s = str(mode).strip().lower()
                if mode_s in ("manual", "hand", "off"):
                    disable_auto = True
                elif mode_s in ("auto", "on"):
                    disable_auto = False
                else:
                    self._json(400, {"error": "invalid mode, expected auto/manual"})
                    return

            if "disable_auto" in payload:
                raw_bool = payload.get("disable_auto")
                if isinstance(raw_bool, bool):
                    disable_auto = raw_bool
                elif isinstance(raw_bool, (int, float)):
                    disable_auto = bool(int(raw_bool))
                elif isinstance(raw_bool, str):
                    b = raw_bool.strip().lower()
                    if b in ("1", "true", "yes", "on", "manual"):
                        disable_auto = True
                    elif b in ("0", "false", "no", "off", "auto"):
                        disable_auto = False
                    else:
                        self._json(400, {"error": "invalid disable_auto value"})
                        return
                else:
                    self._json(400, {"error": "invalid disable_auto type"})
                    return

            min_interval = None
            if "min_interval" in payload and payload.get("min_interval") is not None:
                try:
                    min_interval = int(payload.get("min_interval"))
                except Exception:
                    self._json(400, {"error": "min_interval must be integer"})
                    return
                if not (1 <= min_interval <= 120):
                    self._json(400, {"error": "min_interval out of range (1~120)"})
                    return

            max_interval = None
            if "max_interval" in payload and payload.get("max_interval") is not None:
                try:
                    max_interval = int(payload.get("max_interval"))
                except Exception:
                    self._json(400, {"error": "max_interval must be integer"})
                    return
                if not (1 <= max_interval <= 120):
                    self._json(400, {"error": "max_interval out of range (1~120)"})
                    return

            current = self.shutter_scheduler.status()
            eff_min = min_interval if min_interval is not None else current.get("min_interval")
            eff_max = max_interval if max_interval is not None else current.get("max_interval")
            if (
                eff_min is not None
                and eff_max is not None
                and int(eff_min) > int(eff_max)
            ):
                self._json(400, {"error": "min_interval cannot be greater than max_interval"})
                return

            result = self.shutter_scheduler.update_config(
                disable_auto=disable_auto,
                min_interval=min_interval,
                max_interval=max_interval,
            )
            self._json(200, {"ok": True, "shutter_scheduler": result})
            return

        if path == "/api/terminal/session/start":
            if self.pty_terminal is None:
                self._json(503, {"error": "pty terminal not ready"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            cols = payload.get("cols", 120)
            rows = payload.get("rows", 34)
            self._json(200, self.pty_terminal.start(cols=cols, rows=rows))
            return

        if path == "/api/terminal/session/stop":
            if self.pty_terminal is None:
                self._json(503, {"error": "pty terminal not ready"})
                return
            self._json(200, self.pty_terminal.stop())
            return

        if path == "/api/terminal/session/resize":
            if self.pty_terminal is None:
                self._json(503, {"error": "pty terminal not ready"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                self._json(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "invalid payload"})
                return
            cols = payload.get("cols", 120)
            rows = payload.get("rows", 34)
            self._json(200, self.pty_terminal.resize(cols=cols, rows=rows))
            return

        if path == "/api/terminal/session/write":
            if self.pty_terminal is None:
                self._json(503, {"error": "pty terminal not ready"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                self._json(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "invalid payload"})
                return
            data = payload.get("data", "")
            self._json(200, self.pty_terminal.write(data))
            return

        if path == "/api/open-camera":
            ok, payload = self.manager.open_camera()
            self._json(200 if ok else 500, payload)
            return
        if path == "/api/terminal/exec":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                self._json(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "invalid payload"})
                return
            if self.terminal is None:
                self._json(503, {"error": "terminal not ready"})
                return

            command = payload.get("command", "")
            timeout = TerminalManager._parse_positive_timeout(payload.get("timeout_sec"))
            result = self.terminal.execute(command, timeout_sec=timeout)
            self._json(200, result)
            return
        if path == "/api/palette":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception:
                self._json(400, {"error": "invalid json"})
                return

            name = payload.get("name")
            ok, err = self.manager.set_palette(name)
            if not ok:
                self._json(400, {"error": err, **self.manager.get_palette_info()})
                return
            self._json(200, {"message": "palette updated", **self.manager.get_palette_info()})
            return
        if path == "/api/stop-camera":
            self.manager.stop()
            self._json(200, {"message": "camera stopped"})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="Camera backend for LAN preview")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--shutter-interval-sec",
        type=float,
        default=10.0,
        help="manual shutter refresh interval in seconds while camera is running (<=0 disables)",
    )
    parser.add_argument(
        "--shutter-disable-auto",
        type=int,
        default=0,
        help="disable camera firmware auto shutter via UVC command on startup (1=on, 0=off)",
    )
    parser.add_argument(
        "--shutter-min-interval",
        type=int,
        default=30,
        help="set firmware shutter minimum interval (0-255); applied when auto-shutter policy is configured",
    )
    parser.add_argument(
        "--shutter-max-interval",
        type=int,
        default=90,
        help="set firmware shutter maximum interval (0-255); applied when auto-shutter policy is configured",
    )
    args = parser.parse_args()

    manager = CameraManager()
    camera_vtemp_sampler = CameraVtempSampler()
    metrics = SystemMetricsSampler(camera_vtemp_sampler=camera_vtemp_sampler)
    terminal = TerminalManager()
    pty_terminal = PtyTerminalManager()
    shutter_scheduler = CameraShutterScheduler(
        interval_sec=args.shutter_interval_sec,
        disable_auto=bool(args.shutter_disable_auto),
        min_interval=args.shutter_min_interval,
        max_interval=args.shutter_max_interval,
    )
    shutter_scheduler.start(manager)
    CameraHandler.manager = manager
    CameraHandler.metrics = metrics
    CameraHandler.terminal = terminal
    CameraHandler.pty_terminal = pty_terminal
    CameraHandler.shutter_scheduler = shutter_scheduler
    server = ThreadingHTTPServer((args.host, args.port), CameraHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pty_terminal.stop()
        except Exception:
            pass
        try:
            shutter_scheduler.stop()
        except Exception:
            pass
        manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()

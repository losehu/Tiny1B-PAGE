#!/usr/bin/env python3
import argparse
import glob
import importlib.util
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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


class CameraHandler(BaseHTTPRequestHandler):
    manager: CameraManager = None
    metrics: SystemMetricsSampler = None

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(200, self.manager.get_status())
            return

        if path == "/api/palette":
            self._json(200, self.manager.get_palette_info())
            return

        if path == "/api/metrics":
            camera_status = self.manager.get_status()
            self._json(200, self.metrics.snapshot(camera_status))
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
        if path == "/api/open-camera":
            ok, payload = self.manager.open_camera()
            self._json(200 if ok else 500, payload)
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
    args = parser.parse_args()

    manager = CameraManager()
    camera_vtemp_sampler = CameraVtempSampler()
    metrics = SystemMetricsSampler(camera_vtemp_sampler=camera_vtemp_sampler)
    CameraHandler.manager = manager
    CameraHandler.metrics = metrics
    server = ThreadingHTTPServer((args.host, args.port), CameraHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()

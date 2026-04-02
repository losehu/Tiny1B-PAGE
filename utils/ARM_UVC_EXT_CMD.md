# Tiny1B ARM UVC 扩展命令移植笔记

本文档对应仓库工具：`utils/tiny1b_uvc_cmd.py`

目标：在 ARM 上不依赖 x86 的 `libiruvc.so`，直接通过 USB control transfer 发送 Tiny1B 命令。

## 1. 控制传输参数（逆向结论）

来自 `tiny1B/linux/linux/libiruvc.so`（x86_64）反汇编：

- `std_usb_data_read`:
  - `bmRequestType = 0xC1`（IN | Vendor | Interface）
  - `bRequest = 0x19`
- `std_usb_data_write`:
  - `bmRequestType = 0x41`（OUT | Vendor | Interface）
  - `bRequest = 0x20`

注意参数映射（这点最容易写反）：

- Tiny1B 标准命令层（`tiny1b_standard_cmd_read/write`）传入 `(cmd, index, len, data)`。
- 底层 USB control 实际是：
  - `wValue = index`
  - `wIndex = cmd`

## 2. 分包规则

### 2.1 `tiny1b_standard_cmd_read`

- 单次最大读取：`0xFF`（255）字节
- 超过则循环分包读取并拼接

### 2.2 `tiny1b_standard_cmd_write`

- 单次最大写入：`0x1F`（31）字节
- 超过则循环分包写入
- 零长度写命令会发一个 1 字节 dummy（与原库行为保持一致）

### 2.3 `tiny1b_short_data_rd`（用于 KT/NUCT 大块读）

- 每包最大：`0x20`（32）字节
- 每包请求字段：
  - `wValue = 当前地址`
  - `wIndex = (cmd_hi << 8) | chunk_len`

已知：

- `tpd_kt_get` 对应 `cmd_hi = 0xC2`
- `tpd_nuct_get` 对应 `cmd_hi = 0xC3`

## 3. 关键测温相关命令映射

- `get_ir_sensor_vtemp`:
  - `cmd = 0x0181`
  - `index = 0x0200`
  - `len = 2`
- `get_ir_sensor_flag`:
  - `cmd = 0x018A`
  - `index = 0x0100`
  - `len = 1`
- `tpd_get_env_param`:
  - `cmd = 0x068F`
  - `index = param`
  - `len` 取决于参数：
    - `0x0100`(EMS), `0x0101`(TAU) -> 1 字节
    - `0x0202`(TA), `0x0203`(TU) -> 2 字节

## 4. ARM 实用命令

默认设备：`/dev/video0`

```bash
# 查看视频设备映射到哪个 USB 节点
python3 utils/tiny1b_uvc_cmd.py --pretty info

# 读取 sensor vtemp 原始值（已实测可读）
python3 utils/tiny1b_uvc_cmd.py --pretty vtemp

# 读取环境参数（原始值）
python3 utils/tiny1b_uvc_cmd.py --pretty env-get --param ems
python3 utils/tiny1b_uvc_cmd.py --pretty env-get --param tau
python3 utils/tiny1b_uvc_cmd.py --pretty env-get --param ta
python3 utils/tiny1b_uvc_cmd.py --pretty env-get --param tu

# 手动快门
python3 utils/tiny1b_uvc_cmd.py --pretty shutter-manual

# 通用标准读/写
python3 utils/tiny1b_uvc_cmd.py --pretty cmd-read --cmd-id 0x0181 --index 0x0200 --length 2
python3 utils/tiny1b_uvc_cmd.py --pretty cmd-write --cmd-id 0x0345 --index 0x0000 --data ""

# 读 KT/NUCT 原始块
python3 utils/tiny1b_uvc_cmd.py --pretty kt-get --addr 0 --length 64
python3 utils/tiny1b_uvc_cmd.py --pretty nuct-get --addr 0 --length 64
```

## 5. 当前状态

- ARM 端 UVC vendor 命令链路已打通。
- `get_ir_sensor_vtemp` 已可稳定返回非零原始值。
- 场景“绝对温度”还需要结合 `KT/NUCT/P0-P2/环境参数` 与温度算法库进一步还原。

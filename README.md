# 自动 Ping 工具

基于 Python Tkinter 的轻量级网络诊断工具，专为运维和批量设备调试场景设计。支持设备编号快捷解析、持续 Ping 监控、丢包率统计、MAC 地址解析、一键 VNC 连接、DNS 缓存刷新、**离线批量检测**与**主机-IP 缓存**等功能。

## 功能特性

- **快捷输入**：设备编号（1–999）自动解析为 `APT-LV-SHxxx` 主机名，或直接输入 IP
- **连续 Ping**：持续 Ping 监控，终端风格彩色日志，100ms 超时 + 2 个超时包即判定失败
- **丢包统计**：实时统计发送数 / 成功数 / 丢失数 / 丢包率
- **MAC 地址解析**：Ping 通后通过 `arp -a` 自动解析目标 MAC
- **VNC 一键连接**：回车停止 Ping 并启动 VNC Viewer；使用缓存 IP 即使 Ping 失败也不跳过
- **DNS 刷新**：一键 `ipconfig /flushdns`
- **解析（新增）**：批量解析 `config/list.txt` 中所有设备的 IP 并写入 `config/host.json`
- **离线检测（新增）**：3 轮检测：① 先用缓存 IP → ②③ 失败的条目回退外部 DNS，输出在线/离线报告
- **主机-IP 缓存（新增）**：`config/host.json` 内存缓存优先，外部 DNS 兜底；解析结果自动落盘同步
- **全局热键**：`Alt + Space` 随时唤起窗口到前台（采用 PeekMessageW 50ms 轮询，避免无响应）
- **窗口置顶**：默认非置顶，一键切换
- **紧凑布局**：按钮横向间距 `padx=2`，输出框上边距 `pady=2`
- **无黑框运行**：`.pyw` 入口 + `CREATE_NO_WINDOW` + `STARTUPINFO`，主程序和子进程均不弹控制台
- **日志目录**：`log/debug.log` 自动归档，`.gitignore` 已配置不上传

## 目录结构

```
autoping/
├── main.pyw                  # 入口文件，双击或 pythonw 启动
├── config/
│   ├── list.txt              # 离线检测/解析 目标列表（APT-LV-SHxxx 每行一个）
│   └── host.json             # 主机-IP 缓存（每行一个键值对，自动生成，不上传）
├── log/
│   └── .gitkeep              # debug.log 输出目录，不上传
├── models/
│   └── ping_stats.py         # Ping 统计数据结构
├── scripts/
│   └── batch_ping.py         # 命令行批量 Ping 检测脚本
├── services/
│   ├── host_cache.py         # 核心：host.json 读写 / DNS 解析 / 缓存同步（带 RLock + 原子写入）
│   ├── dns_service.py        # DNS 解析封装（nslookup -qt=A 优先，回退 socket 3 秒超时）
│   ├── network_service.py    # 网络相关工具
│   ├── ping_service.py       # Ping 子进程封装（无黑框 + 100ms 超时）
│   └── vnc_service.py        # VNC Viewer 启动封装
├── ui/
│   └── ping_window.py        # 主窗口 UI + 所有交互逻辑
├── utils/
│   └── validators.pyw        # 输入/IP 校验工具
└── .gitignore
```

## 界面预览

**工具栏按钮顺序（从左到右）**：

```
输入框 + [开始] [结束] [VNC] [刷新DNS] [解析] [离线检测] [置顶]
```

- 黑底绿字终端风格日志区：成功绿色、失败红色、警告黄色
- 紧凑布局：按钮间距 padx=2，上下边距 pady=2

## 使用方法

### 环境要求

- Python 3.x（**全部使用标准库**，无需 pip 安装任何第三方包）
- Windows 系统（推荐；已做防黑框处理；Linux/macOS 可运行但子进程参数适配略有差异）
- （可选）RealVNC Viewer，用于 VNC 一键连接

### 启动

```bash
pythonw main.pyw
```

或直接双击 `main.pyw`（推荐，完全无控制台窗口）。

### 操作说明

| 操作 / 按钮 | 说明 |
| --- | --- |
| 输入 `1` ~ `999` | 自动补全为 `APT-LV-SH001` ~ `APT-LV-SH999` |
| 输入 IP 地址 | 直接 Ping 该 IP |
| 回车（输入框） | 开始 Ping；若 Ping 中 → 停止并启动 VNC（使用最新缓存 IP） |
| 任意键（Ping 中） | 停止当前 Ping |
| `Alt + Space` | 全局热键，窗口唤起并置顶到前台 |
| **开始** | 开始 / 切换 Ping 目标 |
| **结束** | 停止 Ping（首次停止不显示统计摘要） |
| **VNC** | 启动 VNC Viewer 连接最新缓存 IP |
| **刷新DNS** | 执行 `ipconfig /flushdns` |
| **解析** | 读取 `config/list.txt`，DNS 解析所有条目写入 `config/host.json`，仅输出成功/失败统计 |
| **离线检测** | 批量检测 list.txt 在线状态：① 缓存 IP 检测 → ②③ 失败项回退外部 DNS |
| **置顶** | 切换窗口是否始终置顶 |

### 配置列表 `config/list.txt`

每行一个主机名，供「解析」和「离线检测」按钮批量处理：

```
APT-LV-SH015
APT-LV-SH019
APT-LV-SH217
...
```

### 主机缓存 `config/host.json`

格式（每行一个键值对，程序读写自动维护）：

```json
{
"APT-LV-SH015":"192.168.1.15",
"APT-LV-SH019":"",
"APT-LV-SH217":"192.168.1.217"
}
```

- 空字符串 `""` 表示上次解析失败，作为占位避免反复尝试
- 外部 DNS 解析成功会**自动覆盖旧值**
- 旧 IP 失效时，若之前非空则写入空值占位；若本来就是新空条目则不重复写
- 启动时一次性读入内存（`HOST_CACHE` 字典），后续所有操作优先走内存

### DNS 解析优先级

```
1. 内存缓存（host.json）命中直接使用 → 0 延迟
2. 缓存失效/为空 → 子线程异步调用：
     nslookup -qt=A <host> 优先
     失败则回退：socket.gethostbyname(<host>) 超时 3s
3. 解析结果通过 upsert_host_cache_entry() 同步回内存 + 磁盘（原子文件替换防损坏）
```

### VNC 路径配置

在 [services/vnc_service.py](services/vnc_service.py) 中按需修改：

```python
VNC_PATH = r"C:\Program Files\RealVNC\VNC Viewer\vncviewer.exe"
```

默认端口 `5900`。**VNC 启动始终使用最新缓存到的 IP**，即便当前 Ping 失败也不跳过，保证最新解析结果生效。

## 命令行批量检测

```bash
python scripts/batch_ping.py
```

按 `config/list.txt` 列表批量检测，结果同步更新 `config/host.json`。

## 技术要点

| 模块 | 关键实现 |
| --- | --- |
| 防黑框 | `CREATE_NO_WINDOW (0x08000000)` + `STARTUPINFO.dwFlags` 强制隐藏所有子进程窗口 |
| 并发安全 | `threading.RLock` 保护 `HOST_CACHE` 读写；`tempfile` + `os.replace` 原子写入 JSON |
| UI 不阻塞 | DNS 解析 / Ping 启动 一律子线程执行；UI 刷新全部走 `root.after(0, ...)` |
| 全局热键 | `RegisterHotKey` 注册 `Alt+Space`；`PeekMessageW` 50ms 轮询分发消息（避免 `GetMessageW` 造成无响应） |
| Ping 判定 | 超时 100ms，连续 2 个超时包即判定失败；综合 TTL 关键字 + 超时词 + 百分比三重匹配 |
| 平台兼容 | Windows 用 `-n` 编码 GBK；非 Windows 自动切换 `-c` + UTF-8 |

## 忽略规则（.gitignore）

```
config/host.json
log/*
!log/.gitkeep
debug.log
__pycache__/
*.pyc
test_*.py
smoke_*.py
*.tmp
```

## 许可

本项目仅供学习与内部运维使用。

# 自动 Ping 工具

一个基于 Python Tkinter 的轻量级网络诊断工具，专为运维和设备调试场景设计。支持设备编号快捷解析、连续 Ping 监控、丢包率统计、MAC 地址解析、一键 VNC 连接和 DNS 刷新等功能。

## 功能特性

- **快捷输入**：支持输入设备编号（1-999）自动解析为 `APT-LV-SHxxx` 主机名，也支持直接输入 IP 地址
- **连续 Ping**：每秒一次的持续 Ping 监控，带时间戳的彩色日志输出
- **丢包统计**：实时统计发送数、成功数、丢失数和丢包率
- **MAC 地址解析**：Ping 通后自动通过 `arp` 解析目标设备 MAC 地址
- **VNC 一键连接**：回车键即可停止 Ping 并启动 VNC Viewer 连接目标设备
- **DNS 缓存刷新**：一键执行 `ipconfig /flushdns`
- **窗口置顶**：默认置顶方便对照日志，可一键切换
- **无黑框运行**：使用 `.pyw` 后缀 + `CREATE_NO_WINDOW` 双保险，启动和子进程均不弹出控制台窗口

## 界面预览

- 白色简洁工具栏：输入框 + 开始 Ping / 结束 Ping / VNC / 刷新DNS / 取消置顶
- 黑底绿字日志区（终端风格），错误红色、警告黄色、正常绿色

## 使用方法

### 环境要求

- Python 3.x（仅依赖标准库，无需安装任何第三方包）
- Windows 系统（推荐，已做防黑框处理；Linux/macOS 也可运行）

### 运行

```bash
pythonw ping_tool.pyw
```

或直接双击 `ping_tool.pyw` 文件运行（无控制台窗口）。

### 操作说明

| 操作 | 说明 |
| --- | --- |
| 输入 `1` ~ `999` | 自动解析为 `APT-LV-SH001` ~ `APT-LV-SH999` |
| 输入 IP 地址 | 直接 Ping 该 IP |
| 回车（输入框） | 开始 Ping；若正在 Ping，则停止并启动 VNC |
| 任意键（Ping 中） | 停止当前 Ping |
| 「开始 Ping」按钮 | 开始/切换 Ping 目标 |
| 「结束 Ping」按钮 | 停止 Ping 并显示统计 |
| 「VNC」按钮 | 启动 VNC Viewer 连接目标 |
| 「刷新DNS」按钮 | 刷新系统 DNS 缓存 |
| 「取消置顶」按钮 | 切换窗口置顶状态 |

## VNC 路径配置

VNC 客户端路径在 [ping_tool.pyw](ping_tool.pyw) 中以常量形式定义，按需修改：

```python
VNC_PATH = r"C:\\Program Files\\RealVNC\\VNC Viewer\\vncviewer.exe"
```

默认 VNC 端口为 `5900`。

## 技术要点

- **防黑框机制**：`CREATE_NO_WINDOW` (0x08000000) + `STARTUPINFO` 强制隐藏子进程窗口
- **跨线程 UI 更新**：通过 `root.after(0, ...)` 将日志写入调度到主线程
- **平台兼容**：自动识别 Windows / Linux / macOS，使用对应参数（`-n` / `-c`、GBK / UTF-8 编码等）
- **丢包判定**：综合 TTL 存在性、超时关键字和丢包率百分比多重判断

## 许可

本项目仅供学习和内部使用。

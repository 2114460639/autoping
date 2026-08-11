# -*- coding: utf-8 -*-
"""
自动 Ping 工具
【防黑框说明】
- 文件后缀用 .pyw（pythonw.exe 启动 → 启动时无控制台窗口）
- 所有 subprocess.run/Popen 注入 CREATE_NO_WINDOW + STARTUPINFO → 子进程也不弹黑框
"""

import tkinter as tk
from tkinter import ttk, scrolledtext
import subprocess
import threading
import re
import os
import socket
import platform
import time


# ================================================================
# 屏蔽 Windows 子进程控制台黑框的全局参数（仅 Windows 生效）
# ---------------------------------------------------------------
# 1) creationflags = CREATE_NO_WINDOW (0x08000000) — 子进程不创建控制台窗口
# 2) STARTUPINFO.dwFlags |= STARTF_USESHOWWINDOW, wShowWindow = SW_HIDE
#    —— 即使窗口被创建也强制隐藏（双保险）
# ================================================================
if platform.system().lower() == "windows":
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _SW_HIDE = 0
    _STARTF_USESHOWWINDOW = 0x00000001

    _WIN_STARTUPINFO = subprocess.STARTUPINFO()
    _WIN_STARTUPINFO.dwFlags |= _STARTF_USESHOWWINDOW
    _WIN_STARTUPINFO.wShowWindow = _SW_HIDE

    _WIN_SUBPROC_KWARGS = {
        "creationflags": CREATE_NO_WINDOW,
        "startupinfo": _WIN_STARTUPINFO,
    }
else:
    # 非 Windows（Linux/macOS）不需要这些参数
    _WIN_SUBPROC_KWARGS = {}


class PingTool:
    def __init__(self, root):
        self.root = root
        self.root.title("自动 Ping 工具")
        self.root.geometry("800x550")
        self.root.configure(bg="#FFFFFF")

        # ================ 核心变量 ================
        self.hostname = ""
        self.last_hostname = ""
        self.host_mac = "N/A"
        self._mac_resolved = False
        self.ping_running = False
        self.ping_thread = None
        self.stat_total = 0
        self.stat_lost = 0
        self.topmost = True
        self.root.attributes("-topmost", True)

        self._setup_styles()
        self._build_ui()
        self._bind_events()

    # ====================== 样式 ======================
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "White.TButton",
            background="#FFFFFF",
            foreground="#333333",
            font=("Microsoft YaHei UI", 9, "normal"),
            padding=(0, 0),
            relief="solid",
            borderwidth=1,
        )
        style.map(
            "White.TButton",
            background=[("active", "#F0F0F0"), ("pressed", "#E5E5E5")],
            relief=[("pressed", "sunken")],
        )

        style.configure(
            "White.TEntry",
            fieldbackground="#FFFFFF",
            background="#FFFFFF",
            foreground="#333333",
            bordercolor="#E0E0E0",
            lightcolor="#E0E0E0",
            darkcolor="#E0E0E0",
            padding=0,
        )

        style.configure(
            "White.TLabel",
            background="#FFFFFF",
            foreground="#333333",
            font=("Microsoft YaHei UI", 9, "normal"),
        )

    # ====================== UI构建 ======================
    def _build_ui(self):
        top_frame = tk.Frame(self.root, bg="#FFFFFF", padx=12, pady=8)
        top_frame.pack(fill=tk.X)

        ttk.Label(
            top_frame,
            text="设备编号(1-999) 或 IP:",
            style="White.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.entry = ttk.Entry(
            top_frame,
            style="White.TEntry",
            font=("Microsoft YaHei UI", 9, "normal"),
            width=14,
        )
        self.entry.pack(side=tk.LEFT, padx=(0, 2))

        # ===== 按钮顺序：开始 → 结束 → VNC → 刷新DNS → 取消置顶 =====
        self.btn_start = ttk.Button(
            top_frame, text="开始 Ping", style="White.TButton", command=self.start_ping
        )
        self.btn_start.pack(side=tk.LEFT, padx=0)

        self.btn_stop = ttk.Button(
            top_frame, text="结束 Ping", style="White.TButton", command=self.stop_ping
        )
        self.btn_stop.pack(side=tk.LEFT, padx=0)

        self.btn_vnc = ttk.Button(
            top_frame, text="VNC", style="White.TButton", command=self.start_vnc
        )
        self.btn_vnc.pack(side=tk.LEFT, padx=0)

        self.btn_dns = ttk.Button(
            top_frame, text="刷新DNS", style="White.TButton", command=self.flush_dns
        )
        self.btn_dns.pack(side=tk.LEFT, padx=0)

        self.btn_topmost = ttk.Button(
            top_frame,
            text="取消置顶",
            style="White.TButton",
            command=self.toggle_topmost,
        )
        self.btn_topmost.pack(side=tk.LEFT, padx=0)

        # ===== 日志框 =====
        log_wrapper = tk.Frame(self.root, bg="#222222", padx=1, pady=1)
        log_wrapper.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        log_frame = tk.Frame(log_wrapper, bg="#000000")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 10, "normal"),
            bg="#000000",
            fg="#00FF00",
            insertbackground="#00FF00",
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
        )
        self.log_text.tag_configure("ok", foreground="#00FF00")
        self.log_text.tag_configure("err", foreground="#FF2A2A")
        self.log_text.tag_configure("warn", foreground="#FFD700")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    # ====================== 事件绑定 ======================
    def _bind_events(self):
        self.entry.bind("<Return>", self._on_entry_enter)
        self.entry.bind("<Key>", self._on_entry_key)
        self.log_text.bind("<Key>", self._on_log_key)
        self.root.bind("<Key>", self._on_global_key)

    # ====================== 输入解析 ======================
    def _parse_input(self, text):
        text = text.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{1,3}", text):
            num = int(text)
            if 1 <= num <= 999:
                return f"APT-LV-SH{num:03d}"
        ipv4_pattern = r"^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$"
        if re.fullmatch(ipv4_pattern, text):
            return text
        return text if text else None

    # ====================== 键盘事件 ======================
    def _on_entry_enter(self, event):
        if self.ping_running:
            self.stop_ping()
            self.root.after(100, self.start_vnc)
            return "break"

        value = self._parse_input(self.entry.get())
        if not value:
            if self.last_hostname:
                value = self.last_hostname
            else:
                self._append_log("⚠ 输入框为空且没有历史记录，请输入设备编号或IP\n", "warn")
                return "break"

        self.hostname = value
        self.entry.delete(0, tk.END)
        self.start_ping()
        return "break"

    def _on_entry_key(self, event):
        if self.ping_running and event.keysym != "Return":
            self.stop_ping()
            return "break"

    def _on_log_key(self, event):
        if self.ping_running:
            self._handle_ping_keypress(event)
            return "break"

    def _on_global_key(self, event):
        if not self.ping_running:
            return
        self._handle_ping_keypress(event)

    def _handle_ping_keypress(self, event):
        if event.keysym == "Return":
            self.stop_ping()
            self.root.after(100, self.start_vnc)
        else:
            self.stop_ping()

    # ====================== MAC 解析 ======================
    def _resolve_mac(self, host):
        ip = None
        ipv4_pattern = r"^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$"
        if re.fullmatch(ipv4_pattern, host):
            ip = host
        else:
            try:
                ip = socket.gethostbyname(host)
            except Exception:
                return "N/A"
        if not ip:
            return "N/A"

        system = platform.system().lower()
        try:
            if system == "windows":
                cp = subprocess.run(
                    ["arp", "-a", ip],
                    capture_output=True, text=True, encoding="gbk", errors="replace", timeout=3,
                    **_WIN_SUBPROC_KWARGS,   # 🔴 防黑框
                )
                m = re.search(
                    r"([0-9A-Fa-f]{2}[\-\:]){5}[0-9A-Fa-f]{2}",
                    cp.stdout + cp.stderr,
                )
                if m:
                    return m.group(0).upper()
            else:
                cp = subprocess.run(
                    ["arp", "-n", ip],
                    capture_output=True, text=True, errors="replace", timeout=3,
                )
                m = re.search(
                    r"([0-9A-Fa-f]{2}[\-\:]){5}[0-9A-Fa-f]{2}",
                    cp.stdout + cp.stderr,
                )
                if m:
                    return m.group(0).upper()
        except Exception:
            pass
        return "N/A"

    # ====================== Ping 逻辑 ======================
    def start_ping(self):
        if not self.ping_running:
            value = self._parse_input(self.entry.get())
            if not value:
                if self.last_hostname:
                    value = self.last_hostname
                else:
                    self._append_log("⚠ 请输入设备编号(1-999)或IP地址\n", "warn")
                    return
            self.hostname = value
            self.entry.delete(0, tk.END)
        else:
            value = self._parse_input(self.entry.get())
            if not value:
                return
            self.stop_ping(suppress_stats=True)
            self.hostname = value
            self.entry.delete(0, tk.END)

        self.stat_total = 0
        self.stat_lost = 0
        self.host_mac = "N/A"
        self._mac_resolved = False
        self.ping_running = True
        self._append_log(f"\n{'─'*50}\n", "ok")
        self._append_log(f"▶ 开始 Ping: {self.hostname}\n", "ok")
        self._append_log(f"{'─'*50}\n", "ok")

        self.last_hostname = self.hostname

        self.ping_thread = threading.Thread(target=self._ping_loop, daemon=True)
        self.ping_thread.start()

    def stop_ping(self, suppress_stats=False):
        if not self.ping_running:
            return
        self.ping_running = False

        if not suppress_stats:
            total = self.stat_total
            lost = self.stat_lost
            ok = total - lost
            if total > 0:
                rate = (lost / total) * 100
                self._append_log(f"\n{'─'*50}\n", "warn")
                line = f"已发送={total}  成功={ok}  丢失={lost}  丢包率={rate:.1f}%"
                if self.host_mac and self.host_mac != "N/A":
                    line += f"\nMAC={self.host_mac}"
                line += "\n"
                self._append_log(line, "warn")
                self._append_log(f"{'─'*50}\n", "warn")
            else:
                self._append_log("\n■ 本次没有发送任何数据包\n", "warn")

    def _ping_loop(self):
        system = platform.system().lower()
        while self.ping_running:
            timestamp = time.strftime("%H:%M:%S")
            self.stat_total += 1
            is_lost = True
            latency = "?"
            try:
                if system == "windows":
                    cmd = ["ping", "-n", "1", "-w", "1000", self.hostname]
                else:
                    cmd = ["ping", "-c", "1", "-W", "1", self.hostname]

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="gbk" if system == "windows" else "utf-8",
                    errors="replace",
                    timeout=2,
                    **_WIN_SUBPROC_KWARGS,   # 🔴 防黑框（最关键的一处：每秒调用一次）
                )
                output = proc.stdout + proc.stderr
                latency = self._extract_latency(output, system)
                is_lost = self._is_packet_lost(output, system)

                if is_lost:
                    self.stat_lost += 1
                    self._append_log(f"[{timestamp}] 请求超时 → {self.hostname}\n", "err")
                else:
                    if not self._mac_resolved:
                        self.host_mac = self._resolve_mac(self.hostname)
                        self._mac_resolved = True
                    self._append_log(
                        f"[{timestamp}] 响应正常 → {self.hostname}  延迟={latency}\n", "ok"
                    )

            except subprocess.TimeoutExpired:
                self.stat_lost += 1
                self._append_log(f"[{timestamp}] 请求超时 → {self.hostname}\n", "err")
            except Exception as e:
                self.stat_lost += 1
                self._append_log(f"[{timestamp}] 错误: {str(e)}\n", "err")

            for _ in range(10):
                if not self.ping_running:
                    break
                time.sleep(0.1)

    def _extract_latency(self, output, system):
        try:
            if system == "windows":
                m = re.search(r"时间[=<](\d+\s*ms|<1\s*ms)", output)
                if m:
                    return m.group(1).replace(" ", "")
                m2 = re.search(r"平均\s*=\s*(\d+\s*ms)", output)
                if m2:
                    return m2.group(1).replace(" ", "")
            else:
                m = re.search(r"time[=<](\d+(?:\.\d+)?\s*ms)", output)
                if m:
                    return m.group(1).replace(" ", "")
        except Exception:
            pass
        return "?"

    def _is_packet_lost(self, output, system):
        if system == "windows":
            has_ttl = "TTL=" in output or "TTL=" in output.upper()
            timeout_hit = any(
                k in output
                for k in ["请求超时", "找不到主机", "无法访问目标主机"]
            )
            if timeout_hit or not has_ttl:
                if "100%" in output and ("丢失" in output or "lost" in output.lower()):
                    return True
                if timeout_hit:
                    return True
                if not has_ttl:
                    return True
            return False
        else:
            return (
                "100% packet loss" in output.lower()
                or "100.0% packet loss" in output.lower()
            )

    # ====================== VNC 连接 ======================
    def start_vnc(self):
        value = self._parse_input(self.entry.get())
        if value:
            target_host = value
            self.entry.delete(0, tk.END)
        else:
            if self.last_hostname:
                target_host = self.last_hostname
            else:
                self._append_log("⚠ 输入框为空且没有历史记录，无法启动VNC\n", "warn")
                return

        if self.ping_running:
            self.stop_ping()

        host = target_host
        port = 5900
        self.hostname = host
        self.last_hostname = host

        self._append_log(f"\n🚀 启动 VNC 连接: {host}:{port}\n", "ok")

        # ================================================================
        # 🔴 在这里配置你的 VNC 可执行文件路径
        # ================================================================
        VNC_PATH = r"C:\\Program Files\\RealVNC\\VNC Viewer\\vncviewer.exe"
        # ================================================================

        try:
            if not os.path.exists(VNC_PATH):
                self._append_log(f"❌ 找不到VNC程序: {VNC_PATH}\n", "err")
                self._append_log("   请在代码中修改 VNC_PATH 为你的 VNC 客户端实际路径\n", "warn")
                return

            target_addr = f"{host}:{port}"
            # 🔴 防黑框：VNC 也是 GUI 程序，其实不需要 CREATE_NO_WINDOW
            #    但还是统一加上确保万无一失
            subprocess.Popen(
                [VNC_PATH, target_addr],
                shell=False,
                **_WIN_SUBPROC_KWARGS,
            )
            self._append_log(f"✅ 已启动 VNC: {target_addr}\n", "ok")

        except Exception as e:
            self._append_log(f"❌ VNC 启动失败: {str(e)}\n", "err")

    # ====================== 其他按钮 ======================
    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        if self.topmost:
            self.btn_topmost.config(text="取消置顶")
            self._append_log("📌 窗口已置顶\n", "warn")
        else:
            self.btn_topmost.config(text="窗口置顶")
            self._append_log("📌 窗口已取消置顶\n", "warn")

    def flush_dns(self):
        system = platform.system().lower()
        try:
            if system == "windows":
                subprocess.run(
                    ["ipconfig", "/flushdns"],
                    capture_output=True, timeout=5,
                    **_WIN_SUBPROC_KWARGS,   # 🔴 防黑框
                )
                self._append_log("🔄 DNS 缓存已刷新 (ipconfig /flushdns)\n", "warn")
            elif system == "darwin":
                subprocess.run(["dscacheutil", "-flushcache"], capture_output=True, timeout=5)
                subprocess.run(["killall", "-HUP", "mDNSResponder"], capture_output=True, timeout=5)
                self._append_log("🔄 DNS 缓存已刷新 (macOS)\n", "warn")
            else:
                self._append_log("ℹ Linux 刷新DNS需根据发行版手动执行，已跳过\n", "warn")
        except Exception as e:
            self._append_log(f"⚠ DNS 刷新失败: {str(e)}\n", "err")

    # ====================== 日志输出 ======================
    def _append_log(self, text, color="ok"):
        def _do():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, text, color)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)

        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass


def main():
    root = tk.Tk()
    app = PingTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()

import ctypes
import ctypes.wintypes
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import re
import sys

from services.ping_service import create_ping_process
from services.dns_service import flush_dns
from services.vnc_service import open_vnc
from services.network_service import get_mac_address
from utils.validators import parse_hostname
from models.ping_stats import PingStats


BATCH_MAX_WORKERS = 50
BATCH_TIMEOUT = 500

HOTKEY_ID = 1
MOD_WIN = 0x0004
VK_SPACE = 0x20
WM_HOTKEY = 0x0312


def batch_ping_host(host, count=1):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        cmd = [
            "ping",
            "-n", str(count),
            "-w", str(BATCH_TIMEOUT),
            host
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=startupinfo
        )
        output = result.stdout
        alive = "TTL=" in output.upper()
        return alive, output
    except Exception as e:
        return False, str(e)


class PingApp:

    def __init__(self, root):

        self.root = root
        self.root.title("自动 Ping 工具")

        window_width = 720
        window_height = 430

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        x = int((screen_width - window_width) / 2)
        y = int((screen_height - window_height) / 2)

        self.root.geometry(
            f"{window_width}x{window_height}+{x}+{y}"
        )

        self.process = None
        self.hostname = None
        self.host = None

        self.stats = PingStats()
        self.pings_received = False
        self.ping_failed = False

        self.is_topmost = True

        self.batch_running = False
        self.batch_stop_event = threading.Event()
        self.last_log_file = None

        self._hotkey_registered = False
        self._hotkey_thread = None

        self.create_widgets()

        self.root.attributes("-topmost", True)
        self.topmost_btn.config(text="取消置顶")

        self.root.bind(
            "<KeyPress>",
            self.any_key_stop
        )

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.entry.focus_set()

        self._register_hotkey()

    def _register_hotkey(self):
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_thread_func,
            daemon=True
        )
        self._hotkey_thread.start()

    def _hotkey_thread_func(self):
        user32 = ctypes.windll.user32

        if not user32.RegisterHotKey(
            None, HOTKEY_ID, MOD_WIN, VK_SPACE
        ):
            self._hotkey_registered = False
            return

        self._hotkey_registered = True

        msg = ctypes.wintypes.MSG()
        while self._hotkey_registered:
            ret = user32.PeekMessageW(
                ctypes.byref(msg),
                None,
                0,
                0,
                1
            )

            if ret:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.root.after(0, self._show_window)
                elif msg.message == 0x0012:
                    break

            time.sleep(0.05)

        try:
            user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.root.after(200, self._restore_topmost)

    def _restore_topmost(self):
        if self.is_topmost:
            self.root.attributes("-topmost", True)

    def _unregister_hotkey(self):
        if self._hotkey_registered:
            self._hotkey_registered = False
            try:
                ctypes.windll.user32.UnregisterHotKey(
                    None, HOTKEY_ID
                )
            except Exception:
                pass

        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=0.5)

    def on_close(self):
        self._unregister_hotkey()
        self.root.destroy()

    def create_widgets(self):

        top = tk.Frame(self.root)
        top.pack(pady=8)

        tk.Label(
            top,
            text="设备编号(1-999) 或 IP:"
        ).pack(
            side=tk.LEFT,
            padx=5
        )

        self.entry = tk.Entry(
            top,
            width=15
        )

        self.entry.pack(
            side=tk.LEFT
        )

        self.entry.bind(
            "<Return>",
            self.start_ping
        )

        self.start_btn = tk.Button(
            top,
            text="开始 Ping",
            command=self.start_ping
        )

        self.start_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.stop_btn = tk.Button(
            top,
            text="结束 Ping",
            command=self.stop_ping,
            state=tk.DISABLED
        )

        self.stop_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.vnc_btn = tk.Button(
            top,
            text="VNC",
            command=self.do_open_vnc
        )

        self.vnc_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.flushdns_btn = tk.Button(
            top,
            text="刷新DNS",
            command=self.do_flush_dns
        )

        self.flushdns_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.offline_btn = tk.Button(
            top,
            text="离线检测",
            command=self.confirm_offline_check
        )

        self.offline_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.topmost_btn = tk.Button(
            top,
            text="置顶",
            command=self.toggle_topmost
        )

        self.topmost_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.text = tk.Text(
            self.root,
            bg="black",
            fg="lime",
            cursor="arrow"
        )

        self.text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=10,
            pady=(2, 10)
        )

        self.text.config(
            state="disabled"
        )

    def append(self, msg):

        self.text.config(
            state="normal"
        )

        self.text.insert(
            tk.END,
            msg
        )

        self.text.see(tk.END)

        self.text.config(
            state="disabled"
        )

    def get_current_hostname(self):

        value = self.entry.get().strip()

        if value:

            self.hostname = parse_hostname(value)

            return self.hostname

        if self.hostname:

            return self.hostname

        raise ValueError(
            "请输入设备编号、主机名或IP"
        )

    def update_hostname(self):

        self.hostname = parse_hostname(
            self.entry.get()
        )

        return self.hostname

    def start_ping(self, event=None):

        if self.process:

            self.stop_ping()

            if self.pings_received and not self.ping_failed:
                self.do_open_vnc()

            return "break"

        if self.batch_running:
            self.stop_ping()
            return "break"

        try:

            hostname = self.get_current_hostname()
            self.hostname = hostname

            self.entry.delete(
                0,
                tk.END
            )

        except Exception as e:

            messagebox.showerror(
                "错误",
                str(e)
            )

            return "break"

        self.do_ping()

        return "break"

    def do_ping(self):

        self.stats = PingStats()
        self.pings_received = False
        self.ping_failed = False

        self.host = self.hostname

        self.text.config(
            state="normal"
        )

        self.text.delete(
            1.0,
            tk.END
        )

        self.text.config(
            state="disabled"
        )

        self.append(
            f"开始 Ping {self.host}\n\n"
        )

        self.process = create_ping_process(
            self.host
        )

        self.start_btn.config(
            state=tk.DISABLED
        )

        self.stop_btn.config(
            state=tk.NORMAL
        )

        threading.Thread(
            target=self.read_output,
            daemon=True
        ).start()

    def read_output(self):

        if not self.process:
            return

        dns_stopped = False

        try:

            for line in self.process.stdout:

                if not line:
                    continue

                line = line.strip()

                is_dns_fail = (
                    "无法解析" in line
                    or "找不到" in line
                )

                if is_dns_fail and not dns_stopped:
                    dns_stopped = True
                    self.append(
                        f"!!! DNS 未解析到主机，1秒后停止 Ping...\n"
                    )
                    self.root.after(1000, self.stop_ping)

                is_any_result = (
                    "来自" in line
                    or "请求超时" in line
                    or "无法访问目标主机" in line
                )

                if is_any_result:

                    self.stats.sent += 1

                    is_success = (
                        "来自" in line
                        and "无法访问目标主机" not in line
                    )
                    is_failure = (
                        "请求超时" in line
                        or "无法访问目标主机" in line
                    )

                    if is_success:
                        self.stats.received += 1
                        self.pings_received = True

                        m = re.search(r"时间[=<](\d+)ms", line)
                        if m:
                            delay_ms = int(m.group(1))
                            if delay_ms > 1000:
                                self.root.after(
                                    0,
                                    self.append,
                                    f"\n!!! 延迟超过 1000ms ({delay_ms}ms)，自动停止 Ping\n"
                                )
                                self.root.after(0, self.stop_ping)
                                return

                    if is_failure:
                        self.ping_failed = True

                self.root.after(
                    0,
                    self.append,
                    line + "\n"
                )

        except Exception as e:

            self.root.after(
                0,
                self.append,
                f"\n读取输出异常: {e}\n"
            )

        finally:

            if self.process:

                self.process = None

                self.root.after(
                    0,
                    self.show_statistics
                )

    def stop_ping(self):

        if self.batch_running:
            self.batch_stop_event.set()
            self.append("\n正在停止批量检测...\n")
            return

        if not self.process:
            return

        process = self.process

        self.process = None

        try:
            process.kill()

        except:
            pass

        self.show_statistics()

        self.start_btn.config(
            state=tk.NORMAL
        )

        self.stop_btn.config(
            state=tk.DISABLED
        )

    def any_key_stop(self, event):

        if not self.process:
            if not self.batch_running:
                return

        if event.keysym == "Return":
            return

        self.stop_ping()

    def do_flush_dns(self):

        try:

            self.append(
                "\n正在刷新 DNS 缓存...\n"
            )

            result = flush_dns()

            self.append(
                "\n--- DNS 刷新结果 ---\n"
            )

            self.append(
                result + "\n"
            )

        except Exception as e:

            messagebox.showerror(
                "错误",
                f"刷新DNS失败:\n{e}"
            )

    def do_open_vnc(self):

        try:

            hostname = (
                self.get_current_hostname()
            )

            open_vnc(hostname)

            self.append(
                f"\n启动VNC连接: "
                f"{hostname}\n"
            )

        except Exception as e:

            messagebox.showerror(
                "错误",
                f"启动VNC失败:\n{e}"
            )

    def toggle_topmost(self):

        self.is_topmost = (
            not self.is_topmost
        )

        self.root.attributes(
            "-topmost",
            self.is_topmost
        )

        self.topmost_btn.config(
            text=(
                "取消置顶"
                if self.is_topmost
                else "置顶"
            )
        )

    def show_statistics(self):

        if not self.host:
            return

        self.append(
            "\n--- 本次 Ping 统计 ---\n"
        )

        self.append(
            f"主机 = {self.host}\n"
        )

        self.append(
            f"发送 = {self.stats.sent}\n"
        )

        self.append(
            f"接收 = {self.stats.received}\n"
        )

        self.append(
            f"丢失 = {self.stats.lost} "
            f"({self.stats.loss_rate:.1f}% 丢失)\n"
        )

        if self.pings_received and not self.ping_failed:
            ip, mac = get_mac_address(self.host)
            self.append(f"IP 地址 = {ip}\n")
            self.append(f"MAC 地址 = {mac}\n")
        else:
            self.append("未收到有效回复，跳过 MAC 查询\n")

        self.start_btn.config(
            state=tk.NORMAL
        )

        self.stop_btn.config(
            state=tk.DISABLED
        )

    def confirm_offline_check(self):

        if self.batch_running:
            messagebox.showinfo(
                "提示",
                "离线检测正在进行中，请等待完成"
            )
            return

        list_file = Path("list.txt")
        if not list_file.exists():
            messagebox.showerror(
                "错误",
                "未找到 list.txt 文件"
            )
            return

        try:
            with open(list_file, "r", encoding="utf-8") as f:
                hosts = [
                    line.strip()
                    for line in f
                    if line.strip()
                ]
        except Exception as e:
            messagebox.showerror(
                "错误",
                f"读取 list.txt 失败:\n{e}"
            )
            return

        total = len(hosts)

        result = messagebox.askyesno(
            "确认离线检测",
            f"将对 list.txt 中 {total} 台主机进行批量离线检测。\n"
            f"此过程需要几分钟时间，是否继续？"
        )

        if result:
            self.run_offline_check(hosts)

    def run_offline_check(self, hosts):

        self.batch_running = True
        self.batch_stop_event.clear()
        self.offline_btn.config(state=tk.DISABLED)

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        self.text.config(state="normal")
        self.text.delete(1.0, tk.END)
        self.text.config(state="disabled")

        total = len(hosts)

        self.append("=" * 60 + "\n")
        self.append(f"开始离线检测，共 {total} 台主机\n")
        self.append("=" * 60 + "\n\n")

        threading.Thread(
            target=self._batch_check_worker,
            args=(hosts, total),
            daemon=True
        ).start()

    def _batch_check_worker(self, hosts, total):
        offline_hosts = []
        unstable_hosts = []
        batch_lock = threading.Lock()

        def check_host(host):
            host = host.strip()
            if not host:
                return

            if self.batch_stop_event.is_set():
                return

            alive, _ = batch_ping_host(host, 1)
            if alive:
                self.root.after(
                    0, self.append, f"[ONLINE ] {host}\n"
                )
                return

            if self.batch_stop_event.is_set():
                return

            alive, _ = batch_ping_host(host, 1)
            if alive:
                self.root.after(
                    0, self.append, f"[ONLINE ] {host} (Retry)\n"
                )
                return

            if self.batch_stop_event.is_set():
                return

            alive, raw_log = batch_ping_host(host, 3)
            if alive:
                with batch_lock:
                    unstable_hosts.append({
                        "host": host,
                        "log": raw_log
                    })
                self.root.after(
                    0, self.append, f"[UNSTABLE] {host}\n"
                )
                return

            with batch_lock:
                offline_hosts.append(host)
            self.root.after(
                0, self.append, f"[OFFLINE ] {host}\n"
            )

        with ThreadPoolExecutor(max_workers=BATCH_MAX_WORKERS) as pool:
            pool.map(check_host, hosts)

        self.root.after(
            0, self._batch_check_finished,
            hosts, total, offline_hosts, unstable_hosts
        )

    def _batch_check_finished(self, hosts, total, offline_hosts, unstable_hosts):

        log_dir = Path("log")
        log_dir.mkdir(exist_ok=True)

        now = datetime.now()

        log_name = (
            f"{len(offline_hosts)}个离线_"
            f"{now.strftime('%Y%m%d_%H%M%S')}.log"
        )

        log_file = log_dir / log_name

        with open(log_file, "w", encoding="utf-8") as f:

            f.write("=" * 80 + "\n")
            f.write(f"检查时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总主机数：{total}\n")
            f.write(f"不稳定主机：{len(unstable_hosts)}\n")
            f.write(f"离线主机：{len(offline_hosts)}\n")
            f.write("=" * 80 + "\n\n")

            f.write("【离线主机】\n\n")

            if offline_hosts:
                for host in sorted(offline_hosts):
                    f.write(host + "\n")
            else:
                f.write("无\n")

            f.write("\n\n")
            f.write("=" * 80 + "\n")
            f.write("【不稳定主机】\n")
            f.write("=" * 80 + "\n\n")

            if unstable_hosts:

                for item in sorted(
                        unstable_hosts,
                        key=lambda x: x["host"]):

                    f.write(f"主机：{item['host']}\n")
                    f.write("-" * 80 + "\n")
                    f.write(item["log"])
                    f.write("\n")

                    if not item["log"].endswith("\n"):
                        f.write("\n")

                    f.write("=" * 80 + "\n\n")
            else:
                f.write("无\n")

        self.append("\n")
        self.append("=" * 60 + "\n")

        if self.batch_stop_event.is_set():
            self.append("离线检测已手动停止\n")

        self.append("离线检测完成\n")
        self.append(f"总数     : {total}\n")
        self.append(f"离线     : {len(offline_hosts)}\n")
        self.append(f"不稳定   : {len(unstable_hosts)}\n")
        self.append(f"日志文件 : {log_file}\n")
        self.append("=" * 60 + "\n")

        self.last_log_file = log_file
        self._last_offline = offline_hosts
        self._last_unstable = unstable_hosts

        self.batch_running = False
        self.offline_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

        if not self.batch_stop_event.is_set():
            self.root.after(100, self._ask_open_log, log_file)

    def _ask_open_log(self, log_file):
        result = messagebox.askyesno(
            "检测完成",
            f"离线检测完成，共 {len(self._last_offline)} 台离线，"
            f"{len(self._last_unstable)} 台不稳定。\n\n"
            f"是否查看日志文件？"
        )
        if result:
            self._open_file(log_file)

    def _open_file(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror(
                "错误",
                f"无法打开文件: {e}"
            )

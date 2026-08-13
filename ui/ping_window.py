import threading
import tkinter as tk
from tkinter import messagebox

from services.ping_service import create_ping_process
from services.dns_service import flush_dns
from services.vnc_service import open_vnc
from services.network_service import get_mac_address
from utils.validators import parse_hostname
from models.ping_stats import PingStats


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
        
        
        self.is_topmost = False

        self.create_widgets()

        self.root.bind(
            "<KeyPress>",
            self.any_key_stop
        )

        self.entry.focus_set()

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
            padx=5
        )

        self.stop_btn = tk.Button(
            top,
            text="结束 Ping",
            command=self.stop_ping,
            state=tk.DISABLED
        )

        self.stop_btn.pack(
            side=tk.LEFT,
            padx=5
        )

        self.topmost_btn = tk.Button(
            top,
            text="置顶",
            command=self.toggle_topmost
        )

        self.topmost_btn.pack(
            side=tk.LEFT,
            padx=5
        )

        self.flushdns_btn = tk.Button(
            top,
            text="刷新DNS",
            command=self.do_flush_dns
        )

        self.flushdns_btn.pack(
            side=tk.LEFT,
            padx=5
        )

        self.vnc_btn = tk.Button(
            top,
            text="VNC",
            command=self.do_open_vnc
        )

        self.vnc_btn.pack(
            side=tk.LEFT,
            padx=5
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
            pady=10
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
    
        # 优先读取输入框
        if value:
    
            self.hostname = parse_hostname(value)
    
            return self.hostname
    
        # 输入框为空则使用上次记录
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
    
            self.do_open_vnc()
    
            return "break"
    
        try:
    
            hostname = self.get_current_hostname()
            self.hostname = hostname
    
            # 成功解析后清空输入框
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

        try:

            for line in self.process.stdout:

                if not line:
                    continue

                line = line.strip()

                if (
                    "来自" in line
                    or "请求超时" in line
                    or "无法访问目标主机" in line
                ):

                    self.stats.sent += 1

                    if "来自" in line:
                        self.stats.received += 1

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

        ip, mac = get_mac_address(
            self.host
        )

        self.append(
            "\n--- 本次 Ping 统计 ---\n"
        )

        self.append(
            f"主机 = {self.host}\n"
        )

        self.append(
            f"IP 地址 = {ip}\n"
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

        self.append(
            f"MAC 地址 = {mac}\n"
        )

        self.start_btn.config(
            state=tk.NORMAL
        )

        self.stop_btn.config(
            state=tk.DISABLED
        )
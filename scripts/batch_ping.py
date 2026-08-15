import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading

# 项目根目录（scripts/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
LIST_FILE = CONFIG_DIR / "list.txt"

# 在 PROJECT_ROOT 中可导入 services / utils
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.host_cache import (
    resolve_host_cached,
    load_host_cache,
    upsert_host_cache_entry,
)
from utils.validators import parse_hostname

# 确保 config/list.txt 的 host.json 缓存进入内存（模块已导入会自动加载，这里显式调一次确保）
load_host_cache()

# =========================
# 配置
# =========================

MAX_WORKERS = 50      # 并发数
TIMEOUT = 500          # 单次Ping超时(ms)

offline_hosts = []
unstable_hosts = []

lock = threading.Lock()


# =========================
# Ping函数
# =========================
def ping_host(host, count=1):
    try:
        cmd = [
            "ping",
            "-n", str(count),
            "-w", str(TIMEOUT),
            host
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore"
        )

        output = result.stdout

        alive = "TTL=" in output.upper()

        return alive, output

    except Exception as e:
        return False, str(e)


def _normalize(host):
    host = (host or "").strip()
    if not host:
        return None
    try:
        return parse_hostname(host)
    except Exception:
        return host


# =========================
# 主机检查逻辑（与 UI 统一策略：1轮缓存IP，失败→2/3轮外部DNS）
# =========================
def check_host(raw_host):

    raw_host = (raw_host or "").strip()
    if not raw_host:
        return

    norm = _normalize(raw_host)
    display = norm or raw_host

    # 第 1 轮：缓存优先
    t1, used_cache = resolve_host_cached(norm, force_dns=False) if norm else (display, False)
    if not t1:
        t1 = display
    alive, _ = ping_host(t1, 1)

    if alive:
        tag_cached = f" (缓存IP: {t1})" if used_cache and t1 != display else ""
        print(f"[ONLINE ] {display}{tag_cached}")
        return

    # 第 2/3 轮：强制外部 DNS
    t2, _ = resolve_host_cached(norm, force_dns=True) if norm else (None, False)
    if not t2:
        t2 = display

    # 外部 DNS 解析结果落盘：成功填新IP，失败填空值占位
    if norm and t2 != t1:
        if t2 != norm:
            upsert_host_cache_entry(norm, t2)
        else:
            upsert_host_cache_entry(norm, "")

    alive, _ = ping_host(t2, 1)

    if alive:
        if t2 != t1:
            if t2 != norm:
                print(f"[ONLINE ] {display} (Retry, 缓存IP {t1} 未通, 外部DNS: {t2})")
            else:
                print(f"[ONLINE ] {display} (Retry, 缓存IP {t1} 未通, 外部DNS未解析到IP，JSON写空值)")
        else:
            print(f"[ONLINE ] {display} (Retry)")
        return

    alive, raw_log = ping_host(t2, 3)

    if alive:
        with lock:
            unstable_hosts.append({
                "host": display,
                "log": raw_log
            })

        if t2 != norm:
            print(f"[UNSTABLE] {display} (缓存IP {t1} 未通, 外部DNS: {t2})")
        else:
            print(f"[UNSTABLE] {display} (缓存IP {t1} 未通, 外部DNS未解析到IP，JSON写空值)")
        return

    with lock:
        offline_hosts.append(display)

    if t2 != norm:
        print(f"[OFFLINE ] {display} (缓存IP {t1} 未通, 外部DNS: {t2})")
    else:
        print(f"[OFFLINE ] {display} (缓存IP {t1} 未通, 外部DNS未解析到IP，JSON写空值)")


# =========================
# 主程序
# =========================
def main():

    list_file = LIST_FILE

    if not list_file.exists():
        print(f"未找到 {list_file}")
        return

    with open(list_file, "r", encoding="utf-8") as f:
        hosts = [
            line.strip()
            for line in f
            if line.strip()
        ]

    total = len(hosts)

    print("=" * 60)
    print(f"开始检查，共 {total} 台主机")
    print(f"主机清单: {list_file}")
    print("规则: 第1轮=缓存IP；第1轮失败 → 第2/3轮=外部DNS后再Ping")
    print("=" * 60)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pool.map(check_host, hosts)

    # 创建日志目录
    log_dir = PROJECT_ROOT / "log"
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
        f.write(f"主机清单：{list_file}\n")
        f.write(f"总主机数：{total}\n")
        f.write(f"不稳定主机：{len(unstable_hosts)}\n")
        f.write(f"离线主机：{len(offline_hosts)}\n")
        f.write("=" * 80 + "\n\n")

        # 离线主机
        f.write("【离线主机】\n\n")

        if offline_hosts:
            for host in sorted(offline_hosts):
                f.write(host + "\n")
        else:
            f.write("无\n")

        # 不稳定主机
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

    print("\n")
    print("=" * 60)
    print("检查完成")
    print(f"总数     : {total}")
    print(f"离线     : {len(offline_hosts)}")
    print(f"不稳定   : {len(unstable_hosts)}")
    print(f"日志文件 : {log_file}")
    print("=" * 60)


if __name__ == "__main__":
    main()
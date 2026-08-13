import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading

# 项目根目录（scripts/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

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


# =========================
# 主机检查逻辑
# =========================
def check_host(host):

    host = host.strip()

    if not host:
        return

    # 第一轮
    alive, _ = ping_host(host, 1)

    if alive:
        print(f"[ONLINE ] {host}")
        return

    # 第二轮
    alive, _ = ping_host(host, 1)

    if alive:
        print(f"[ONLINE ] {host} (Retry)")
        return

    # 第三轮
    alive, raw_log = ping_host(host, 3)

    if alive:
        with lock:
            unstable_hosts.append({
                "host": host,
                "log": raw_log
            })

        print(f"[UNSTABLE] {host}")
        return

    with lock:
        offline_hosts.append(host)

    print(f"[OFFLINE ] {host}")


# =========================
# 主程序
# =========================
def main():

    list_file = PROJECT_ROOT / "list.txt"

    if not list_file.exists():
        print("未找到 list.txt")
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
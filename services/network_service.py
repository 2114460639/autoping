import socket
import subprocess


def get_ip(hostname):

    try:
        return socket.gethostbyname(hostname)
    except:
        return hostname


def get_mac_address(hostname):

    try:

        ip = get_ip(hostname)

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.check_output(
            ["arp", "-a", ip],
            text=True,
            encoding="gbk",
            errors="ignore",
            startupinfo=startupinfo
        )

        for line in result.splitlines():

            if ip in line:

                parts = line.split()

                if len(parts) >= 2:
                    return ip, parts[1]

        return ip, "未找到（ARP未缓存/跨网段）"

    except Exception as e:

        return "未知", f"获取失败: {e}"
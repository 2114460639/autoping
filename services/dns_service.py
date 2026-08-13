import subprocess


def flush_dns():

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    return subprocess.check_output(
        ["ipconfig", "/flushdns"],
        text=True,
        encoding="gbk",
        errors="ignore",
        startupinfo=startupinfo
    )
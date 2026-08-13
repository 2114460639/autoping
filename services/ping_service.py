import subprocess


def create_ping_process(host):

    return subprocess.Popen(
        [
            "ping",
            host,
            "-t",
            "-4",
            "-w",
            "1000"
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

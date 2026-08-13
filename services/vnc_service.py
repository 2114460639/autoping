import subprocess

VNC_PATH = (
    r"C:\Program Files\RealVNC"
    r"\VNC Viewer\vncviewer.exe"
)

def open_vnc(hostname):

    subprocess.Popen(
        [
            VNC_PATH,
            hostname
        ]
    )
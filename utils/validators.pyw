import re

def valid_ip(ip):
    try:
        parts = ip.split(".")
        if len(parts) != 4:
            return False

        for part in parts:
            if not part.isdigit():
                return False

            if not (0 <= int(part) <= 255):
                return False

        return True

    except:
        return False


def parse_hostname(value):

    value = value.strip()

    if not value:
        raise ValueError("请输入设备编号、主机名或IP")

    if value.isdigit():

        num = int(value)

        if not (1 <= num <= 999):
            raise ValueError("请输入1-999编号")

        return f"APT-LV-SH{num:03d}"

    if re.fullmatch(
        r"APT-LV-SH\d{3}",
        value.upper()
    ):
        return value.upper()

    if valid_ip(value):
        return value

    raise ValueError(
        "请输入1-999编号、APT-LV-SHxxx或完整IP"
    )
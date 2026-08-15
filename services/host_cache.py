"""
设备 DNS 缓存服务：
- 启动时把 config/host.json 读入内存常量 HOST_CACHE（key=标准化主机名, value=IP字符串，失败条目保持空串）
- JSON 文件格式：单行紧凑对象 {"APT-LV-SH001":"192.168.1.1","APT-LV-SH999":""}
- 解析按钮：读取 config/list.txt，将其中设备全部外部DNS解析一遍并写回 host.json（解析失败写空值 ""）
- 对外：
    * resolve_host_cached(hostname, force_dns=False) —— 缓存优先 / 强制外部 DNS
    * upsert_host_cache_entry(hostname, ip_or_empty) —— 单条写入内存+立即落盘（成功时替换旧IP；失败时保持空值等下次解析成功再覆盖）
"""

import json
import re
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock

try:
    from utils.validators import parse_hostname
except Exception:  # 单独调试时兼容
    parse_hostname = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
LIST_FILE = CONFIG_DIR / "list.txt"
HOST_JSON = CONFIG_DIR / "host.json"

# 解析并发（按 list.txt 规模）
RESOLVE_MAX_WORKERS = 100

_LOCK = RLock()


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 1. 启动期加载：内存常量 HOST_CACHE
# ============================================================
HOST_CACHE = {}   # type: dict[str, str]


def load_host_cache() -> dict:
    """启动时从 config/host.json 加载；文件不存在时返回 {}，不报错。"""
    global HOST_CACHE
    _ensure_config_dir()
    data = {}
    if HOST_JSON.is_file():
        try:
            with open(HOST_JSON, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(k, str):
                        nk = k.strip()
                        if v is None:
                            nv = ""
                        elif isinstance(v, str):
                            nv = v.strip()
                        else:
                            # 兼容其他类型转字符串
                            nv = str(v).strip()
                        if nk:
                            data[nk] = nv
        except Exception:
            # JSON 坏了就从空开始，解析按钮重新生成即可
            data = {}
    with _LOCK:
        HOST_CACHE = data
    return data


def _dump_cache_to_disk(data: dict):
    """把内存字典写回 host.json：每行一个键值对，保持对象结构 + 排序。"""
    _ensure_config_dir()
    tmp_path = HOST_JSON.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            # 每行一个键值对：{ 单独一行；"KEY":"VALUE", 单独一行；} 单独一行
            keys = sorted(data.keys())
            f.write("{\n")
            for i, k in enumerate(keys):
                v = data.get(k, "") or ""
                line = f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(v, ensure_ascii=False)}"
                if i < len(keys) - 1:
                    line += ","
                f.write(line + "\n")
            f.write("}\n")
        tmp_path.replace(HOST_JSON)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


# ============================================================
# 2. 外部 DNS 解析
# ============================================================
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _looks_like_ip_raw(v: str) -> bool:
    if not _IPV4_RE.fullmatch((v or "").strip()):
        return False
    for p in (v or "").strip().split("."):
        if not 0 <= int(p) <= 255:
            return False
    return True


def dns_resolve(hostname: str, timeout: float = 3.0):
    """
    外部 DNS 解析：优先 nslookup（纯外部 DNS 服务器，和系统缓存无关），失败再回退 socket.gethostbyname。
    为了避免 gethostbyname 在解析失败时卡 2~12s，整体用子线程超时包装，超时也返回 None。
    """
    h = (hostname or "").strip()
    if not h:
        return None
    if _looks_like_ip_raw(h):
        return h

    result_container = {"ip": None}

    def worker():
        # 1) nslookup：取最后一个 Name/Address 段下面的 Address 对应 IP（忽略 127.0.0.* 本机 DNS）
        try:
            proc = subprocess.run(
                ["nslookup", "-qt=A", h],
                capture_output=True, text=True, encoding="gbk", errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=timeout,
            )
            text = proc.stdout or ""
            addrs = []
            for ln in text.splitlines():
                # nslookup 输出有两种：
                #   "名称:    xxx\r\n地址:  x.x.x.x"
                #   或者 IP 直接出现在 "Addresses:" / "Address:" 冒号后面
                if "名称:" in ln or "Name:" in ln:
                    addrs = []  # 下一段 Address 属于目标域名
                    continue
                m = re.search(r"(?:地址|Addresses|Address)\s*:\s*((?:\d{1,3}\.){3}\d{1,3})", ln)
                if m:
                    ip = m.group(1)
                    if not ip.startswith("127."):
                        addrs.append(ip)
            if addrs:
                result_container["ip"] = addrs[-1]
                return
        except Exception:
            pass

        # 2) socket.gethostbyname 回退
        try:
            result_container["ip"] = socket.gethostbyname(h)
        except Exception:
            result_container["ip"] = None

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result_container["ip"]


# ============================================================
# 3. 对外：解析主机名 -> IP（带缓存优先策略）
# ============================================================
def resolve_host_cached(hostname, force_dns=False):
    """
    返回 (resolved_target, used_cache)
        - force_dns=False: 只查缓存 + parse_hostname 列表，绝不做阻塞外部DNS；
                           命中 HOST_CACHE 非空 => 返回该 IP, used_cache=True
                           输入本身已是 IP => 返回该 IP, used_cache=True
                           其它情况 => 返回标准化后的主机名, used_cache=False（让上层按需异步解析）
        - force_dns=True:  忽略缓存，强制外部 DNS 解析（可能阻塞）。
    DNS 失败仍返回原 hostname（让 ping 自己报错）。
    """
    if parse_hostname is None:
        host = (hostname or "").strip()
    else:
        if _looks_like_ip(hostname if isinstance(hostname, str) else ""):
            host = hostname.strip() if isinstance(hostname, str) else ""
        else:
            try:
                host = parse_hostname(hostname)
            except Exception:
                host = (hostname or "").strip()

    if not host:
        return "", False

    if _looks_like_ip(host):
        # 本身就是 IP：直接用，不走外部 DNS
        return host, True

    if not force_dns:
        cached_ip = _cache_lookup(host)
        if cached_ip:
            return cached_ip, True
        # 缓存未命中：绝不做阻塞 DNS，返回 host 让上层按需在 worker 线程里 force_dns=True 解析
        return host, False

    # force_dns=True：走外部 DNS 兜底（会阻塞）
    ip = dns_resolve(host)
    if ip:
        return ip, False
    return host, False


def _looks_like_ip(v: str) -> bool:
    parts = v.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        n = int(p)
        if not (0 <= n <= 255):
            return False
    return True


def _cache_lookup(host: str):
    """查找缓存，命中非空值才返回（空值表示"上次解析失败"，不能当IP用）。"""
    if not host:
        return None
    if _looks_like_ip(host):
        return host
    with _LOCK:
        cache = HOST_CACHE
    v = cache.get(host)
    if v:
        return v
    return None


def get_cached_value(hostname, include_empty: bool = False):
    """
    取当前内存缓存值：
      - include_empty=False: 只有命中非空才返回，否则 None（等于 IP 可用才返回）
      - include_empty=True : 命中任何值都返回，不存在则 None（用于"原值是否为空"判断）
    """
    key = normalize_host_key(hostname)
    if not key:
        return None
    with _LOCK:
        v = HOST_CACHE.get(key, None)
    if v is None:
        return None
    if v == "" and not include_empty:
        return None
    return v


# ============================================================
# 4. 单条写入：外部解析/缓存回退/离线检测等场景触发 —— 新IP写回 JSON，替换旧IP
# ============================================================
def normalize_host_key(hostname) -> str:
    """把输入标准化成 JSON 里的键（parse_hostname 优先；失败则原字串，空串为None）。"""
    h = (hostname or "").strip()
    if not h:
        return ""
    if _looks_like_ip(h):
        return h
    if parse_hostname is not None:
        try:
            return parse_hostname(h)
        except Exception:
            return h
    return h


def upsert_host_cache_entry(hostname, ip_or_empty) -> dict:
    """
    单条更新：
      - hostname: 设备编号/主机名/IP（内部会 normalize 成标准 key）
      - ip_or_empty: 解析成功传IP字符串；失败传空字符串 "" 或 None（失败保留空值，等下次成功覆盖）
    返回 {"changed":bool, "key":str, "old":str, "new":str}
    """
    key = normalize_host_key(hostname)
    if not key:
        return {"changed": False, "key": "", "old": "", "new": ""}
    # IP 规范化
    if ip_or_empty is None:
        new_val = ""
    else:
        new_val = str(ip_or_empty).strip()
    # 如果写了非空IP，校验下格式；格式不对就当写入失败，存空值避免误用
    if new_val and not _looks_like_ip(new_val):
        new_val = ""

    with _LOCK:
        key_exists = key in HOST_CACHE
        old_val = HOST_CACHE.get(key, "")
        unchanged = key_exists and (old_val == new_val)
        if unchanged:
            return {"changed": False, "key": key, "old": old_val, "new": new_val}
        new_data = dict(HOST_CACHE)
        new_data[key] = new_val
        # 先更内存，保证后续 resolve_host_cached 立即生效（即便落盘失败也至少内存生效）
        HOST_CACHE.clear()
        HOST_CACHE.update(new_data)

    # 落盘放到锁外，减少锁占用时长；_LOCK 只保护内存读写
    try:
        _dump_cache_to_disk(new_data)
    except Exception:
        # 落盘失败仍可返回 changed=True，内存已更新，调用方可打印日志
        pass
    return {"changed": True, "key": key, "old": old_val, "new": new_val}


# ============================================================
# 5. 「解析」按钮：读取 list.txt -> 外部 DNS 全量解析 -> 写回 host.json
#    规则：解析成功写IP；失败写空字符串（保持key存在，便于下次覆盖）
# ============================================================
def resolve_all_list(max_workers: int = RESOLVE_MAX_WORKERS):
    _ensure_config_dir()
    if not LIST_FILE.is_file():
        return {
            "ok": False,
            "total": 0,
            "success": 0,
            "failed": 0,
            "list_file": str(LIST_FILE),
            "output": f"未找到 {LIST_FILE}",
            "mapping": {},
        }

    try:
        with open(LIST_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception as e:
        return {
            "ok": False,
            "total": 0,
            "success": 0,
            "failed": 0,
            "list_file": str(LIST_FILE),
            "output": f"读取 list.txt 失败: {e}",
            "mapping": {},
        }

    items = []
    for raw in lines:
        if _looks_like_ip(raw):
            items.append((raw, raw))
            continue
        if parse_hostname is None:
            items.append((raw, raw))
            continue
        try:
            norm = parse_hostname(raw)
        except Exception:
            norm = None
        items.append((raw, norm))

    # 本轮结果：成功=IP, 失败=空字符串；最终 merged = 旧JSON ∪ 本轮结果
    this_round = {}
    success = 0
    failed = 0
    out_lines = []

    def _job(norm_key, raw_line):
        if _looks_like_ip(norm_key):
            return norm_key, norm_key, True
        ip = dns_resolve(norm_key)
        return norm_key, ip, bool(ip)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for raw, norm in items:
            if norm is None:
                # 格式无法识别 → 当作失败写入空值（但key无法构造，只好记失败）
                out_lines.append(f"[SKIP   ] {raw}  -> 格式无法识别，跳过")
                failed += 1
                continue
            if norm in futures:
                continue
            futures[norm] = pool.submit(_job, norm, raw)

        for norm, fut in futures.items():
            try:
                _k, ip, ok = fut.result()
            except Exception as e:
                out_lines.append(f"[FAIL   ] {norm}  -> 解析异常: {e}")
                this_round[norm] = ""
                failed += 1
                continue
            if ok:
                this_round[_k] = ip
                success += 1
                out_lines.append(f"[OK     ] {_k:<18s} -> {ip}")
            else:
                # 失败写空值，等下次解析成功再写入
                this_round[_k] = ""
                failed += 1
                out_lines.append(f"[FAIL   ] {_k:<18s} -> DNS 解析失败，JSON中保留空值")

    # 合并：旧JSON ∪ 本轮结果（本轮结果覆盖旧值，失败的key也被写成""覆盖旧IP）
    old = {}
    if HOST_JSON.is_file():
        try:
            with open(HOST_JSON, "r", encoding="utf-8") as f:
                raw_old = json.load(f)
            if isinstance(raw_old, dict):
                for k, v in raw_old.items():
                    if isinstance(k, str):
                        if v is None:
                            old[k] = ""
                        elif isinstance(v, str):
                            old[k] = v.strip()
                        else:
                            old[k] = str(v).strip()
        except Exception:
            old = {}

    merged = dict(old)
    merged.update(this_round)

    try:
        _dump_cache_to_disk(merged)
    except Exception as e:
        # 写盘失败也尽量把缓存先放进内存，界面能先用到
        with _LOCK:
            HOST_CACHE.clear()
            HOST_CACHE.update(merged)
        out_lines.append("")
        out_lines.append(f"写回 host.json 失败: {e}")
        return {
            "ok": False,
            "total": len(items),
            "success": success,
            "failed": failed,
            "list_file": str(LIST_FILE),
            "output": "\n".join(out_lines),
            "mapping": merged,
        }

    # 更新内存常量：原地替换内容（避免 global 声明）
    with _LOCK:
        HOST_CACHE.clear()
        HOST_CACHE.update(merged)

    out_lines.append("")
    out_lines.append(
        f"解析完成：共 {len(items)} 条，成功 {success}，失败 {failed}（失败条目在JSON中写空值）"
    )
    out_lines.append(f"合并后缓存条目: {len(merged)}，已写入 {HOST_JSON}")

    return {
        "ok": True,
        "total": len(items),
        "success": success,
        "failed": failed,
        "list_file": str(LIST_FILE),
        "output": "\n".join(out_lines),
        "mapping": merged,
    }


# 模块加载即载入一次（即使外部不主动调，也保证常量内存就位）
load_host_cache()

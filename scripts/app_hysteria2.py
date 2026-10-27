#!/usr/bin/env python3
"""
KataBump 单端口复用版:Hysteria2 (QUIC/UDP) + Python Web 面板 (TCP)

这个文件是 script/app.py 的 Hysteria2 替代版,自包含、无外部依赖,
可以直接当作 app.py 使用(启动方式与环境变量保持一致)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
架构:为什么和原版不一样
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
原版 app.py 用一个 TCP socket 读首字节分诊:TLS -> REALITY,其他 -> 面板。

本版做不到,原因是 Hysteria2 跑在 QUIC/UDP 上,没有 "TLS 首字节" 这一层,
无法和 TCP 面板挤进同一个 socket。

但端口号可以复用 —— TCP 和 UDP 是两套完全独立的协议栈:
    公网端口 (SERVER_PORT)
      ├── TCP  → 本进程 ThreadingHTTPServer → MyBox 面板
      └── UDP  → hysteria2 子进程 (QUIC, 伪装成 HTTP/3)

所以对外只需要一个端口号,客户端填的也是同一个端口号。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠ 唯一前提:平台必须放行 UDP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hysteria2 只有 UDP,没有 TCP 回退。如果平台只映射了 TCP,
日志会一切正常,但客户端永远连不上 —— 而且不会有任何报错。

先用这条命令确认(在你自己的电脑上跑,不是服务器上):

    HY_UDP_ECHO=1 python3 app.py

脚本会进入 UDP 回声模式(不启动代理),然后在你电脑上执行:

    python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(5);s.sendto(b'ping',('<服务器IP>',<端口>));print('UDP OK:',s.recvfrom(64))"

收到 b'ping' 就说明 UDP 通,去掉 HY_UDP_ECHO 正常启动即可;
超时则说明 UDP 被封,这台机器上用不了 Hysteria2。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
环境变量(端口获取逻辑与原版完全一致)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    SERVER_PORT / RR_PORT / PORT   公网端口(按此优先级,必需)
    SERVER_IP / RR_SERVER_ADDRESS  公网地址(必需)
    APP_TOKEN                      面板与 WebDAV 令牌(默认沿用原版值)
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  配置后 /notify 转发 Telegram

    HY_VERSION              hysteria 版本 tag(默认 app/v2.13.0)
    HY_PASSWORD             认证密码(不设则自动生成并持久化)
    HY_OBFS_PASSWORD        设置后启用 salamander 混淆
    HY_SNI                  证书 CN / 客户端 SNI(默认 www.bing.com)
    HY_MASQUERADE_URL       masquerade 反代目标(默认 https://www.bing.com)
    HY_CERT / HY_KEY        使用已有证书(不设则自动生成自签证书)
    HY_REGENERATE=1         重建密码与证书(旧链接失效)
    HY_NODE_NAME            节点显示名
    HY_UDP_ECHO=1           只做 UDP 回声自检,不启动代理
    HY_DEBUG=1              打印面板访问日志

依赖: 无。自签证书由 hysteria 自带的 `cert` 子命令生成,不需要 openssl。
      (hysteria cert 万一失败才会回退到系统 openssl;也可以直接用
      HY_CERT / HY_KEY 指定已有证书)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import fcntl
except ImportError:  # 非 Linux(本地开发测试)
    fcntl = None


# ---------------------------------------------------------------------------
# 版本与路径
# ---------------------------------------------------------------------------

DEFAULT_VERSION = "app/v2.13.0"
VERSION = os.environ.get("HY_VERSION", DEFAULT_VERSION).strip()

REPOSITORY = "HyNetworks/hysteria"
ASSET = "hysteria-linux-amd64"

RELEASE_BASE = f"https://github.com/{REPOSITORY}/releases/download/{VERSION}"
ASSET_URL = f"{RELEASE_BASE}/{ASSET}"
HASHES_URL = f"{RELEASE_BASE}/hashes.txt"

HOME = Path(os.environ.get("HOME", "/home/container"))
STATE_DIR = HOME / ".hysteria2-node"
BINARY = STATE_DIR / "hysteria"
CONFIG = STATE_DIR / "config.yaml"
CLIENT_META = STATE_DIR / "client.json"
CLIENT_LINK = STATE_DIR / "client-link.txt"
CERT_FILE = STATE_DIR / "cert.pem"
KEY_FILE = STATE_DIR / "key.pem"
LOCK_FILE = STATE_DIR / ".bootstrap.lock"

MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
HTTP_TIMEOUT = 30

APP_TOKEN = "lwj123456"
WEB_TOKEN = os.environ.get("APP_TOKEN", "").strip() or APP_TOKEN

MAX_UPLOAD = 200 * 1024 * 1024  # Web 单文件上限

DATA_DIR = Path(__file__).resolve().parent / "data"
FILE_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "notes.db"


class BootstrapError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------
# 端口规划:TCP 面板与 UDP 代理共用同一个端口号(与原版取值逻辑一致)
# ---------------------------------------------------------------------------

def _public_port_raw() -> int:
    raw = os.environ.get("RR_PORT") or os.environ.get("SERVER_PORT") or os.environ.get("PORT")
    if not raw:
        raise RuntimeError("SERVER_PORT is missing")
    value = int(raw)
    if not 1 <= value <= 65535:
        raise RuntimeError(f"port outside 1..65535: {value!r}")
    return value


try:
    PUBLIC_PORT = _public_port_raw()
except Exception:
    PUBLIC_PORT = 0  # 环境不完整时允许 import(便于本地测试),main() 里再校验


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def run(args: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"command timed out after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise BootstrapError(f"cannot execute {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise BootstrapError(
            f"command failed ({result.returncode}): {' '.join(args)}" + (f"\n{detail}" if detail else "")
        )
    return result


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": f"katabump-hysteria2/{VERSION}", "Accept": "*/*"}
    )


def fetch_text(url: str, max_bytes: int = 1024 * 1024) -> str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(_request(url), timeout=HTTP_TIMEOUT) as response:
                data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise BootstrapError(f"response exceeds {max_bytes} bytes: {url}")
            return data.decode("utf-8")
        except Exception as exc:
            last_error = exc
            if attempt != 2:
                time.sleep(1 << attempt)
    raise BootstrapError(f"failed to fetch {url}: {last_error}")


def download(url: str, destination: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        partial = destination.with_name(destination.name + ".part")
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        try:
            total = 0
            with urllib.request.urlopen(_request(url), timeout=HTTP_TIMEOUT) as source, open(partial, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise BootstrapError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if total == 0:
                raise BootstrapError("download returned an empty file")
            os.replace(partial, destination)
            return
        except Exception as exc:
            last_error = exc
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
            if attempt != 2:
                time.sleep(1 << attempt)
    raise BootstrapError(f"failed to download {url}: {last_error}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 二进制安装(hysteria 的 release 是裸二进制,不是 tar.gz)
# ---------------------------------------------------------------------------

def release_asset_sha256() -> str | None:
    """从 hashes.txt 取目标资产的 SHA256;拿不到返回 None,由调用方降级。"""
    try:
        sums = fetch_text(HASHES_URL)
    except Exception as exc:
        log(f"warning: cannot fetch hashes.txt ({exc}); falling back to binary self-check")
        return None
    for raw_line in sums.splitlines():
        fields = raw_line.strip().split()
        if len(fields) < 2:
            continue
        cleaned = [f.lstrip("*") for f in fields]
        if ASSET not in cleaned:
            continue
        for field in cleaned:
            if field == ASSET:
                continue
            if re.fullmatch(r"[0-9a-fA-F]{64}", field):
                return field.lower()
    log(f"warning: {ASSET} not listed in hashes.txt; falling back to binary self-check")
    return None


def verify_static_x86_64_elf(path: Path) -> None:
    with open(path, "rb") as stream:
        header = stream.read(64)
    if len(header) < 64 or header[:4] != b"\x7fELF":
        raise BootstrapError("release binary is not ELF")
    if header[4] != 2 or header[5] != 1:
        raise BootstrapError("release binary is not little-endian ELF64")
    e_machine = struct.unpack_from("<H", header, 18)[0]
    if e_machine != 62:
        raise BootstrapError(f"release binary architecture is not x86_64 (e_machine={e_machine})")


def binary_is_usable() -> bool:
    if not BINARY.is_file():
        return False
    try:
        verify_static_x86_64_elf(BINARY)
        os.chmod(BINARY, 0o755)
        # hysteria 的 version 输出是多行块("Version:\t..."),不含 "hysteria" 字样
        result = run([str(BINARY), "version"], timeout=10)
        return "Version:" in (result.stdout or "")
    except Exception:
        return False


def install_binary() -> None:
    if binary_is_usable():
        return
    log(f"installing hysteria {VERSION} (linux-amd64)")
    expected = release_asset_sha256()
    archive = STATE_DIR / ASSET
    download(ASSET_URL, archive)
    actual = sha256_file(archive)
    if expected is not None and actual != expected:
        with contextlib.suppress(FileNotFoundError):
            archive.unlink()
        raise BootstrapError(
            f"release binary SHA256 mismatch: expected={expected} actual={actual}"
        )
    fd, tmp_name = tempfile.mkstemp(prefix=".hysteria.", suffix=".tmp", dir=STATE_DIR)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as target, open(archive, "rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(tmp, 0o755)
        verify_static_x86_64_elf(tmp)
        os.replace(tmp, BINARY)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    with contextlib.suppress(FileNotFoundError):
        archive.unlink()
    if not binary_is_usable():
        raise BootstrapError("the hysteria binary cannot execute in this container")


# ---------------------------------------------------------------------------
# 自签证书
# ---------------------------------------------------------------------------

def certificate_fingerprint(path: Path) -> str:
    """证书 DER 的 SHA-256,小写十六进制 —— 即 URI 里的 pinSHA256。

    以 hysteria 客户端源码为准(app/cmd/client.go):
        hashHex := hex.EncodeToString(sha256.Sum256(cert)[:])
        if hashHex == normalizeCertHash(PinSHA256) { ok }
    normalizeCertHash 只做小写化并去掉 ':' 和 '-'。
    所以这里必须是 hex,不能用 base64 —— 用 base64 会被判为不匹配并拒绝连接。
    """
    pem = path.read_text("ascii")
    body = "".join(
        line.strip() for line in pem.splitlines() if line and not line.startswith("-----")
    )
    der = base64.b64decode(body)
    return hashlib.sha256(der).hexdigest()


def _generate_cert_with_hysteria(sni: str) -> None:
    """用 hysteria 自带的 cert 子命令生成自签证书(不需要 openssl)。"""
    for path in (CERT_FILE, KEY_FILE):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    run(
        [
            str(BINARY), "cert",
            "--host", sni,
            "--cert", str(CERT_FILE),
            "--key", str(KEY_FILE),
            "--valid-for", "87600h",  # 10 年
            "--overwrite",
        ],
        timeout=30,
    )
    if not CERT_FILE.is_file() or not KEY_FILE.is_file():
        raise BootstrapError("hysteria cert did not produce the expected files")
    os.chmod(KEY_FILE, 0o600)
    os.chmod(CERT_FILE, 0o644)


def _generate_cert_with_openssl(sni: str) -> None:
    """备选:系统 openssl 生成自签证书。"""
    for path in (CERT_FILE, KEY_FILE):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "3650",
            "-keyout", str(KEY_FILE),
            "-out", str(CERT_FILE),
            "-subj", f"/CN={sni}",
            "-addext", f"subjectAltName=DNS:{sni}",
        ],
        timeout=30,
    )
    os.chmod(KEY_FILE, 0o600)
    os.chmod(CERT_FILE, 0o644)


def ensure_certificate(sni: str) -> str:
    # 用户自带证书优先
    env_cert = os.environ.get("HY_CERT", "").strip()
    env_key = os.environ.get("HY_KEY", "").strip()
    if env_cert and env_key:
        cert_path, key_path = Path(env_cert), Path(env_key)
        if not cert_path.is_file() or not key_path.is_file():
            raise BootstrapError("HY_CERT / HY_KEY points to a missing file")
        return certificate_fingerprint(cert_path)

    if CERT_FILE.is_file() and KEY_FILE.is_file():
        try:
            return certificate_fingerprint(CERT_FILE)
        except Exception as exc:
            log(f"warning: existing certificate unreadable ({exc}); regenerating")
            for path in (CERT_FILE, KEY_FILE):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    log(f"generating self-signed certificate (CN={sni})")
    # 首选 hysteria 自带 cert 命令:不依赖系统 openssl,
    # 生成的 pinSHA256 也是 hex,与本脚本算法一致。
    try:
        _generate_cert_with_hysteria(sni)
        return certificate_fingerprint(CERT_FILE)
    except Exception as exc:
        log(f"hysteria cert failed ({exc}); falling back to openssl")

    if shutil.which("openssl") is None:
        raise BootstrapError(
            "cannot generate a self-signed certificate: the bundled 'hysteria cert' "
            "command failed and openssl is not installed.\n"
            "Provide your own certificate via HY_CERT / HY_KEY, or install openssl."
        )
    _generate_cert_with_openssl(sni)
    return certificate_fingerprint(CERT_FILE)


# ---------------------------------------------------------------------------
# 节点身份与配置生成
# ---------------------------------------------------------------------------

def public_host() -> str:
    value = (os.environ.get("RR_SERVER_ADDRESS") or os.environ.get("SERVER_IP") or "").strip()
    value = value.strip("[]")
    if not value:
        raise BootstrapError("SERVER_IP is missing; set RR_SERVER_ADDRESS")
    return value


def yaml_str(value: str) -> str:
    """把任意字符串安全地放进 YAML 双引号标量。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config(port: int, password: str, obfs_password: str | None) -> str:
    masquerade_url = os.environ.get("HY_MASQUERADE_URL", "https://www.bing.com").strip()
    cert_path = Path(os.environ.get("HY_CERT", "").strip() or CERT_FILE)
    key_path = Path(os.environ.get("HY_KEY", "").strip() or KEY_FILE)
    lines = [
        "# 由 app.py(hysteria2 版)生成,重启会覆盖,请勿手工修改",
        f"listen: :{port}",
        "",
        "tls:",
        f"  cert: {yaml_str(str(cert_path))}",
        f"  key: {yaml_str(str(key_path))}",
        "  sniGuard: disable",
        "",
        "auth:",
        "  type: password",
        f"  password: {yaml_str(password)}",
    ]
    if obfs_password:
        lines += [
            "",
            "obfs:",
            "  type: salamander",
            "  salamander:",
            f"    password: {yaml_str(obfs_password)}",
        ]
    lines += [
        "",
        "masquerade:",
        "  type: proxy",
        "  proxy:",
        f"    url: {yaml_str(masquerade_url)}",
        "    rewriteHost: true",
        "",
        "# 自用场景:服务端不设 bandwidth,由客户端自己声明(官方推荐)",
        "congestion:",
        "  type: bbr",
        "  bbrProfile: standard",
        "",
        "quic:",
        "  maxIdleTimeout: 30s",
        "  maxIncomingStreams: 1024",
        "",
    ]
    return "\n".join(lines)


def validate_config(path: Path) -> None:
    """hysteria 没有 check 子命令,只能在短命进程里试启动来验证配置。

    配置非法会立刻非零退出;配置合法则会持续运行,被超时杀掉属于预期。
    """
    try:
        result = subprocess.run(
            [str(BINARY), "server", "--config", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        return  # 一直活着 = 配置通过
    except OSError as exc:
        raise BootstrapError(f"cannot execute hysteria: {exc}") from exc
    detail = ((result.stderr or "") + (result.stdout or "")).strip()
    if len(detail) > 4000:
        detail = detail[-4000:]
    raise BootstrapError(
        f"hysteria rejected the generated config (rc={result.returncode})"
        + (f"\n{detail}" if detail else "")
    )


def load_or_create_node(port: int) -> dict:
    if os.environ.get("HY_REGENERATE") == "1":
        log("HY_REGENERATE=1: deleting persisted password/certificate (old links will stop working)")
        for path in (CONFIG, CLIENT_META, CLIENT_LINK, CERT_FILE, KEY_FILE):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    sni = os.environ.get("HY_SNI", "www.bing.com").strip() or "www.bing.com"
    obfs_password = os.environ.get("HY_OBFS_PASSWORD", "").strip() or None

    meta: dict = {}
    if CLIENT_META.exists():
        try:
            meta = json.loads(CLIENT_META.read_text("utf-8"))
        except Exception:
            raise BootstrapError(
                "persisted client metadata is unreadable. Set HY_REGENERATE=1 once to rebuild it."
            )

    password = (
        os.environ.get("HY_PASSWORD", "").strip()
        or meta.get("password")
        or secrets.token_urlsafe(24)
    )
    fingerprint = ensure_certificate(meta.get("sni") or sni)

    config_text = render_config(port, password, obfs_password)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".yaml", dir=STATE_DIR)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(config_text)
            stream.flush()
            os.fsync(stream.fileno())
        validate_config(tmp)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    atomic_write(CONFIG, config_text.encode())

    meta = {
        "version": VERSION,
        "password": password,
        "obfsPassword": obfs_password,
        "sni": sni,
        "pinSHA256": fingerprint,
    }
    atomic_write(CLIENT_META, (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode())
    return meta


def hysteria2_link(meta: dict, host: str, port: int) -> str:
    try:
        parsed_ip = ipaddress.ip_address(host)
        authority_host = f"[{host}]" if parsed_ip.version == 6 else host
    except ValueError:
        authority_host = host

    query: dict[str, str] = {"sni": meta["sni"], "insecure": "1"}
    if meta.get("obfsPassword"):
        query["obfs"] = "salamander"
        query["obfs-password"] = meta["obfsPassword"]
    if meta.get("pinSHA256"):
        query["pinSHA256"] = meta["pinSHA256"]

    auth = urllib.parse.quote(meta["password"], safe="")
    node_name = urllib.parse.quote(os.environ.get("HY_NODE_NAME", "KataBump-hysteria2"), safe="")
    # quote_via=quote:urlencode 默认用 quote_plus,会把空格变 '+',
    # 而客户端对 query 值只做 percent-decode,'+' 会被当成字面加号。
    query_string = urllib.parse.urlencode(query, safe="", quote_via=urllib.parse.quote)
    return f"hysteria2://{auth}@{authority_host}:{port}/?{query_string}#{node_name}"


# ---------------------------------------------------------------------------
# MyBox Web 面板(纯标准库,行为与原版一致)
# ---------------------------------------------------------------------------

def safe_name(name: str) -> str:
    return os.path.basename(name).replace("\\", "_").strip() or "unnamed"


def web_db() -> sqlite3.Connection:
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    return conn


def send_telegram(text: str) -> str:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (bot and chat):
        return "telegram not configured (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    payload = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return "sent"
    except Exception as e:
        return f"telegram error: {e}"


def parse_multipart(body: bytes, content_type: str):
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return []
    boundary = m.group(1).encode()
    parts = []
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        header_blob, _, data = chunk.partition(b"\r\n\r\n")
        headers = header_blob.decode("utf-8", "replace")
        fname = re.search(r'filename="([^"]*)"', headers)
        if fname:
            parts.append((safe_name(fname.group(1)), data))
    return parts


INDEX_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Box</title>
<style>
 body{max-width:720px;margin:40px auto;padding:0 16px;font-family:system-ui,sans-serif;color:#222}
 h1{font-size:1.5em} section{margin:2em 0;padding:1em;border:1px solid #ddd;border-radius:8px}
 input,textarea,button{font-size:1em;padding:6px}
 #list,#notes{list-style:none;padding-left:0}
 li{margin:6px 0;word-break:break-all}
 .del{color:#c00;cursor:pointer;margin-left:8px}
 button{cursor:pointer}
</style></head><body>
<h1>My Box</h1>
<p>单端口复用:这个 HTTP 面板走 TCP,代理走同一端口号的 UDP(Hysteria2)。</p>

<section><h2>文件传输</h2>
<input type="file" id="f"><button onclick="upload()">上传</button>
<p id="up"></p>
<ul id="list"></ul></section>

<section><h2>笔记</h2>
<textarea id="t" rows="3" style="width:100%" placeholder="随手记一条…"></textarea>
<button onclick="addNote()">保存</button>
<ul id="notes"></ul></section>

<section><h2>WebDAV 挂载</h2>
<p>手机/电脑文件管理器可直接挂载本站 <code>/dav/</code> 目录(协议 WebDAV,支持子目录)。
示例:安卓 Solid Explorer / iOS Documents 添加服务器 → WebDAV → 地址 <code>http://服务器IP:端口/dav/</code>。
设置 APP_TOKEN 后,密码填 APP_TOKEN,用户名任意。</p></section>

<script>
const qs = new URLSearchParams(location.search);
const token = qs.get('token') || '';

async function loadFiles(){
  const r = await fetch('/files?json=1');
  const files = await r.json();
  document.getElementById('list').innerHTML = files.map(f =>
    `<li><a href="/files/${encodeURIComponent(f.name)}?token=${token}">${f.name}</a>
     <small>(${(f.size/1024).toFixed(1)} KB)</small>
     <span class="del" onclick="delFile('${encodeURIComponent(f.name)}')">[删]</span></li>`).join('');
}
async function upload(){
  const file = document.getElementById('f').files[0];
  if(!file) return;
  const fd = new FormData(); fd.append('file', file);
  const r = await fetch('/upload?token='+token, {method:'POST', body:fd});
  document.getElementById('up').textContent = await r.text();
  loadFiles();
}
async function delFile(name){
  await fetch('/files/'+name+'?token='+token, {method:'DELETE'});
  loadFiles();
}
async function loadNotes(){
  const r = await fetch('/notes');
  const notes = await r.json();
  document.getElementById('notes').innerHTML = notes.map(n =>
    `<li><small>${n.created}</small><br>${n.text.replace(/</g,'&lt;')}</li>`).join('');
}
async function addNote(){
  const t = document.getElementById('t').value.trim();
  if(!t) return;
  await fetch('/notes?token='+token, {method:'POST', body:t});
  document.getElementById('t').value = '';
  loadNotes();
}
loadFiles(); loadNotes();
</script></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "MyBox/1.2"
    protocol_version = "HTTP/1.1"  # WebDAV 客户端需要 keep-alive

    def log_message(self, fmt, *args):
        if os.environ.get("HY_DEBUG"):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _authed(self):
        if not WEB_TOKEN:
            return True
        if "token=" in self.path and self.path.split("token=", 1)[-1].split("&")[0] == WEB_TOKEN:
            return True
        if self.headers.get("X-Token") == WEB_TOKEN:
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD + 65536:
            raise ValueError("payload too large")
        return self.rfile.read(length)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML)
        if path == "/health":
            return self._json({"status": "ok"})
        if path == "/dav" or path.startswith("/dav/"):
            t = self._dav_target()
            if t is None:
                return self._send(403, b"")
            real, href = t
            if not os.path.exists(real):
                return self._send(404, b"")
            if os.path.isfile(real):
                data = real.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                name = urllib.parse.quote(real.name)
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(data)
                return
            rows = []
            for name in sorted(os.listdir(real), key=str.lower):
                child = os.path.join(real, name)
                chref = href + urllib.parse.quote(name) + ("/" if os.path.isdir(child) else "")
                label = html.escape(name) + ("/" if os.path.isdir(child) else "")
                rows.append(f'<li><a href="{chref}">{label}</a></li>')
            listing = ("<html><head><meta charset='utf-8'><title>MyBox</title></head><body>"
                       f"<h1>/dav/</h1><ul>{''.join(rows)}</ul>"
                       "<p><a href='/'>返回主页</a></p></body></html>")
            return self._send(200, listing)
        if path == "/files":
            items = []
            for name in sorted(os.listdir(FILE_DIR)):
                p = os.path.join(FILE_DIR, name)
                if os.path.isfile(p):
                    items.append({"name": name, "size": os.path.getsize(p)})
            return self._json(items)
        if path.startswith("/files/"):
            name = safe_name(urllib.parse.unquote(path[len("/files/"):]))
            p = os.path.join(FILE_DIR, name)
            if not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            with open(p, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(name)}"')
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/notes":
            with web_db() as conn:
                rows = conn.execute("SELECT id, text, created FROM notes ORDER BY id DESC").fetchall()
            return self._json([{"id": r[0], "text": r[1], "created": r[2]} for r in rows])
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            return
        if path == "/upload":
            ctype = self.headers.get("Content-Type", "")
            body = self._body()
            saved = []
            if "multipart/form-data" in ctype:
                for name, data in parse_multipart(body, ctype):
                    if not data:
                        continue
                    with open(os.path.join(FILE_DIR, name), "wb") as f:
                        f.write(data)
                    saved.append(name)
            else:
                name = safe_name(
                    urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("name", ["upload.bin"])[0]
                )
                with open(os.path.join(FILE_DIR, name), "wb") as f:
                    f.write(body)
                saved.append(name)
            return self._json({"saved": saved})
        if path == "/notes":
            text = self._body().decode("utf-8", "replace").strip()
            if not text:
                return self._json({"error": "empty"}, 400)
            with web_db() as conn:
                conn.execute("INSERT INTO notes (text) VALUES (?)", (text,))
            return self._json({"ok": True})
        if path == "/notify":
            text = self._body().decode("utf-8", "replace").strip()
            if not text:
                text = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("text", [""])[0]
            if not text:
                return self._json({"error": "empty"}, 400)
            return self._json({"result": send_telegram(text)})
        return self._json({"error": "not found"}, 404)

    # ---------- WebDAV(挂载点 /dav/,与网页文件区共用 FILE_DIR)----------

    def _dav_target(self):
        """把 /dav/ 下的 URL 解析为 (文件系统路径, href);拒绝越界,返回 None 表示非法"""
        path = urllib.parse.urlparse(self.path).path
        rel = urllib.parse.unquote(path)
        if rel.rstrip("/") == "/dav":
            parts = []
        elif rel.startswith("/dav/"):
            parts = [p for p in rel[len("/dav/"):].split("/") if p not in ("", ".")]
        else:
            return None
        if any(p == ".." for p in parts):
            return None
        real = FILE_DIR.joinpath(*parts) if parts else FILE_DIR
        try:
            if os.path.commonpath([str(FILE_DIR.resolve()), str(Path(real).resolve())]) != str(FILE_DIR.resolve()):
                return None
        except Exception:
            return None
        href = "/dav/" + "/".join(urllib.parse.quote(p) for p in parts)
        if not parts:
            href = "/dav/"
        elif os.path.isdir(real):
            href += "/"
        return real, href

    def _dav_authed(self):
        """WebDAV 鉴权:Basic 密码 = APP_TOKEN(用户名任意);也接受 ?token= / X-Token"""
        if not WEB_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _user, _, pwd = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
            except Exception:
                pwd = ""
            if pwd == WEB_TOKEN:
                return True
        if "token=" in self.path and self.path.split("token=", 1)[-1].split("&")[0] == WEB_TOKEN:
            return True
        if self.headers.get("X-Token") == WEB_TOKEN:
            return True
        body = b'{"error": "unauthorized"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="MyBox"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _no_content(self):
        self.send_response(204)
        self.end_headers()

    def _discard_body(self):
        with contextlib.suppress(Exception):
            length = int(self.headers.get("Content-Length") or 0)
            while length > 0:
                chunk = self.rfile.read(min(262144, length))
                if not chunk:
                    break
                length -= len(chunk)

    def _dav_prop(self, href, is_dir, size=None, mtime=None):
        modified = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(mtime or time.time()))
        if is_dir:
            prop = "<D:resourcetype><D:collection/></D:resourcetype>"
        else:
            prop = f"<D:resourcetype/><D:getcontentlength>{size or 0}</D:getcontentlength>"
        return (
            "<D:response>"
            f"<D:href>{href}</D:href>"
            "<D:propstat><D:prop>"
            f"{prop}"
            f"<D:getlastmodified>{modified}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response>"
        )

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("DAV", "1")
        self.send_header("MS-Author-Via", "DAV")
        self.send_header("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, MKCOL")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        t = self._dav_target()
        if t and os.path.isfile(t[0]):
            real = t[0]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(real)))
            self.end_headers()
        else:
            self._send(404, b"")

    def do_PROPFIND(self):
        if not self._dav_authed():
            return
        t = self._dav_target()
        if t is None:
            return self._send(403, b"")
        real, href = t
        self._discard_body()
        if not os.path.exists(real):
            return self._send(404, b"")
        depth = (self.headers.get("Depth") or "1").strip()
        responses = []
        if os.path.isdir(real):
            responses.append(self._dav_prop(href, True, None, os.path.getmtime(real)))
            if depth != "0":
                for name in sorted(os.listdir(real)):
                    child = os.path.join(real, name)
                    is_dir = os.path.isdir(child)
                    chref = href + urllib.parse.quote(name) + ("/" if is_dir else "")
                    responses.append(self._dav_prop(
                        chref, is_dir,
                        None if is_dir else os.path.getsize(child),
                        os.path.getmtime(child),
                    ))
        else:
            responses.append(self._dav_prop(href, False, os.path.getsize(real), os.path.getmtime(real)))
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>")
        self._send(207, body, "text/xml; charset=utf-8")

    def do_PUT(self):
        if not self._dav_authed():
            return
        t = self._dav_target()
        if t is None:
            return self._send(403, b"")
        real, _href = t
        if os.path.isdir(real):
            return self._json({"error": "is a directory"}, 409)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._json({"error": "payload too large"}, 413)
        existed = real.exists()
        try:
            real.parent.mkdir(parents=True, exist_ok=True)
            with open(real, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(262144, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as exc:
            return self._json({"error": str(exc)}, 500)
        self._send(204 if existed else 201, b"")

    def do_MKCOL(self):
        if not self._dav_authed():
            return
        t = self._dav_target()
        if t is None:
            return self._send(403, b"")
        real, _href = t
        self._discard_body()
        if real.exists():
            return self._send(405, b"")
        try:
            real.mkdir(parents=False)
        except FileNotFoundError:
            return self._send(409, b"")
        self._send(201, b"")

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/dav" or path.startswith("/dav/"):
            if not self._dav_authed():
                return
            t = self._dav_target()
            if t is None:
                return self._send(403, b"")
            real, _href = t
            if not os.path.exists(real) or real == FILE_DIR:
                return self._send(404, b"")
            if os.path.isdir(real):
                shutil.rmtree(real, ignore_errors=True)
            else:
                os.remove(real)
            return self._no_content()
        if not self._authed():
            return
        if path.startswith("/files/"):
            name = safe_name(urllib.parse.unquote(path[len("/files/"):]))
            p = os.path.join(FILE_DIR, name)
            if os.path.isfile(p):
                os.remove(p)
                return self._json({"deleted": name})
            return self._json({"error": "not found"}, 404)
        return self._json({"error": "not found"}, 404)


def start_web_server() -> ThreadingHTTPServer:
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PUBLIC_PORT), WebHandler)
    thread = threading.Thread(target=server.serve_forever, name="mybox-web", daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# UDP 可达性自检(Hysteria2 的前提条件)
# ---------------------------------------------------------------------------

def udp_echo_selftest() -> None:
    """只绑定 UDP 端口并原样回包,用来确认平台是否真的放行 UDP。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", PUBLIC_PORT))
    except OSError as exc:
        raise BootstrapError(f"cannot bind UDP {PUBLIC_PORT}: {exc}") from exc

    print()
    print("=" * 78)
    print("UDP 回声自检模式(HY_UDP_ECHO=1)")
    print(f"已监听 udp/0.0.0.0:{PUBLIC_PORT},收到任何数据都会原样回发。")
    print()
    print("现在在【你自己的电脑】上执行(把 IP 和端口换成实际值):")
    print()
    print('  python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);'
          "s.settimeout(5);s.sendto(b'ping',('<服务器IP>',"
          f"{PUBLIC_PORT}));print('UDP OK:',s.recvfrom(64))\"")
    print()
    print("收到 b'ping' → UDP 通,去掉 HY_UDP_ECHO 正常启动即可。")
    print("一直超时      → UDP 被拦,这台机器用不了 Hysteria2,请换 TCP 类协议。")
    print("=" * 78)
    print()
    sys.stdout.flush()

    while True:
        try:
            data, peer = sock.recvfrom(2048)
        except KeyboardInterrupt:
            break
        except OSError:
            break
        log(f"udp probe from {peer[0]}:{peer[1]} ({len(data)} bytes) -> echoed")
        with contextlib.suppress(OSError):
            sock.sendto(data, peer)
    sock.close()


# ---------------------------------------------------------------------------
# hysteria2 子进程守护
# ---------------------------------------------------------------------------

async def supervise_hysteria(stop: asyncio.Event) -> None:
    env = os.environ.copy()
    proc: subprocess.Popen | None = None
    try:
        while not stop.is_set():
            proc = subprocess.Popen(
                [str(BINARY), "server", "--config", str(CONFIG)],
                env=env,
            )
            log(f"hysteria2 serving on udp/0.0.0.0:{PUBLIC_PORT} (pid {proc.pid})")
            while proc.poll() is None and not stop.is_set():
                await asyncio.sleep(1)
            if stop.is_set():
                break
            log(f"hysteria2 exited (rc={proc.returncode}), restarting in 3s")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=3)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()


async def serve() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    start_web_server()
    log(f"web panel listening on tcp/0.0.0.0:{PUBLIC_PORT}")

    # 必须先把 hysteria 守护任务建起来,否则代理永远不会启动
    supervisor = asyncio.create_task(supervise_hysteria(stop))

    await stop.wait()
    await asyncio.gather(supervisor, return_exceptions=True)
    log("shutdown complete")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    if sys.platform != "linux":
        raise BootstrapError("Linux is required")
    if os.uname().machine != "x86_64":
        raise BootstrapError("this bootstrap requires x86_64")

    global PUBLIC_PORT
    PUBLIC_PORT = _public_port_raw()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)

    # UDP 自检模式:不装二进制、不起代理,只回答"UDP 到底通不通"
    if os.environ.get("HY_UDP_ECHO") == "1":
        udp_echo_selftest()
        return

    host = public_host()

    lock_fh = None
    if fcntl is not None:
        lock_fh = open(LOCK_FILE, "a+b")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

    try:
        install_binary()
        meta = load_or_create_node(PUBLIC_PORT)
        link = hysteria2_link(meta, host, PUBLIC_PORT)
        atomic_write(CLIENT_LINK, (link + "\n").encode())

        web_token_hint = "?token=<APP_TOKEN>" if WEB_TOKEN else ""
        print()
        print("=" * 78)
        print(f"hysteria2 {VERSION} + MyBox | 单端口号复用 | TCP=面板 UDP=代理")
        print(f"server  : {host}:{PUBLIC_PORT}")
        print(f"web     : http://{host}:{PUBLIC_PORT}/{web_token_hint}")
        print(f"SNI     : {meta['sni']}")
        if meta.get("obfsPassword"):
            print("obfs    : salamander")
        print()
        print("提示:如果客户端连不上但日志无报错,几乎一定是 UDP 没放行。")
        print("      用 HY_UDP_ECHO=1 启动可确认。")
        print()
        print("COPY THIS LINK INTO v2rayN / sing-box / NekoBox:")
        print()
        print(link)
        print("=" * 78)
        print()
        sys.stdout.flush()

        asyncio.run(serve())
    finally:
        if lock_fh is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            lock_fh.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

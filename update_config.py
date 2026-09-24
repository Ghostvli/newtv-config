#!/usr/bin/env python3
"""在线接口解密 + ZTE 合并工具。

流程：
  1. 下载在线接口（伪装成图片的文件：JPEG 数据 + "标记**Base64配置"）
  2. 解出明文配置 JSON
  3. 剔除 remove_site_keys 列出的站点，前置 zte.json 里的 ZTE 站点和直播源
  4. 与现有输出做语义对比，有实质变化才写入

安全约束：
  - 仅允许 http/https；目标 host 拒绝 localhost/.local/内网/环回/保留地址
  - 连接使用校验时解析出的同一 IP（防 DNS rebinding），重定向每一跳重新校验
  - 读写路径规范化，禁止 ..，输出限制在允许目录（当前目录/用户主目录）内

仅用标准库，本地与 GitHub Actions 均可直接运行：
  python3 update_config.py --url http://www.xn--sss604efuw.cc/tv \
      --output NewTV.json --zte zte.json
"""

import argparse
import base64
import http.client
import ipaddress
import json
import os
import pathlib
import socket
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

CNY = timezone(timedelta(hours=8))
# 接口按 UA 分流：浏览器 UA 返回导航页，播放器 UA（okhttp 等）才返回藏配置的图片
UA = "okhttp/4.9.3"

# 输出文件允许的根目录：仓库所在目录（CI）或用户主目录（本地手动更新到桌面等）
ALLOWED_ROOTS = [
    pathlib.Path(os.getcwd()).resolve(),
    pathlib.Path.home().resolve(),
]


def _host_allowed(host: str) -> bool:
    """host 字符串级校验：非 localhost/.local；字面 IP 必须是公网地址。"""
    low = host.lower().rstrip(".")
    if low == "localhost" or low.endswith(".local") or low.endswith(".internal"):
        return False
    try:
        return ipaddress.ip_address(low).is_global
    except ValueError:
        return True  # 域名，交给解析后的 IP 校验


def _resolve_global(host: str, port: int) -> str:
    """DNS 解析并要求全部结果为公网地址，返回钉住的 IP。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addrs or not all(a.is_global for a in addrs):
        raise ValueError(f"host {host} 解析结果含非公网地址: {[str(a) for a in addrs]}")
    return str(addrs[0])


def check_url(url: str) -> None:
    """请求前校验：仅 http/https，host 非本地/保留地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https，实际 scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少 host")
    if not _host_allowed(host):
        raise ValueError(f"拒绝本地/保留地址: {host}")


# ---- 钉住已校验 IP 的连接（校验与连接用同一解析结果，防 DNS rebinding） ----

class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        ip = _resolve_global(self.host, self.port)
        self.sock = self._create_connection((ip, self.port), self.timeout, self.source_address)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        ip = _resolve_global(self.host, self.port)
        self.sock = self._create_connection((ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # 每一跳重定向都重新校验
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> bytes:
    check_url(url)
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.UnknownHandler())
    opener.add_handler(_SafeRedirectHandler())
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    # 覆盖连接实现，使 TCP 连接全部走钉住 IP 的版本
    opener.http_open = lambda req: urllib.request.HTTPHandler.do_open(_PinnedHTTPConnection, req)
    opener.https_open = lambda req: urllib.request.HTTPSHandler.do_open(_PinnedHTTPSConnection, req)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as resp:
        return resp.read()


def _reject_dotdot(path: str) -> None:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"路径不允许包含 ..: {path}")


def _inside_allowed_roots(real: pathlib.Path) -> bool:
    return any(real == root or root in real.parents for root in ALLOWED_ROOTS)


def safe_read_path(path: str) -> pathlib.Path:
    """规范化读取路径，禁止 .. 穿越，且必须已存在。"""
    _reject_dotdot(path)
    real = pathlib.Path(path).expanduser().resolve()
    if not real.is_file():
        raise FileNotFoundError(f"文件不存在: {real}")
    return real


def safe_write_path(path: str) -> pathlib.Path:
    """规范化输出路径：禁止 ..，限制在允许目录内，父目录必须已存在。"""
    _reject_dotdot(path)
    real = pathlib.Path(path).expanduser().resolve()
    if not _inside_allowed_roots(real):
        raise ValueError(f"输出路径必须位于允许目录内 {[str(r) for r in ALLOWED_ROOTS]}: {path}")
    if not real.parent.is_dir():
        raise ValueError(f"输出目录不存在: {real.parent}")
    return real


def b64decode_lenient(text: str) -> bytes:
    cleaned = "".join(text.split())
    return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))


def strip_comments(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def extract_config(raw: bytes) -> dict:
    """从接口原始数据中解出配置 JSON，兼容明文/图片藏配置/Base64 三种形态。"""
    # 1) 直接就是明文 JSON（可能带 // 注释）
    if raw.lstrip()[:1] == b"{":
        return json.loads(strip_comments(raw.decode("utf-8")))

    # 2) 图片藏配置：取 JPEG EOI 之后的 "标记**Base64" 段
    text = None
    eoi = raw.rfind(b"\xff\xd9")
    if eoi != -1 and eoi + 2 < len(raw):
        tail_head = raw[eoi + 2:eoi + 34].decode("latin-1", "replace").lstrip()
        if not tail_head.startswith("{") and "**" in tail_head:
            text = raw[eoi + 2:].decode("utf-8", "replace")
    if text is None:
        # 3) 整体就是 Base64 文本
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("无法识别的数据格式：既不是 JSON、图片藏配置，也不是 Base64")

    marker = text.find("**")
    payload = text[marker + 2:] if marker != -1 else text
    decoded = b64decode_lenient(payload).decode("utf-8")
    return json.loads(strip_comments(decoded))


def merge(remote: dict, zte: dict) -> dict:
    cfg = dict(remote)
    drop = set(zte.get("remove_site_keys", []))
    cfg["sites"] = zte.get("sites", []) + [
        s for s in remote.get("sites", [])
        if s.get("key") not in drop and s.get("api") not in drop
    ]
    cfg["lives"] = zte.get("lives", []) + remote.get("lives", [])
    return cfg


def render(cfg: dict, source_url: str) -> str:
    header = (
        f"//      >>> 自动更新于：{datetime.now(CNY).strftime('%Y-%m-%d %H:%M:%S')} <<<\n"
        f"//     由 GitHub Actions 定时解密生成，请勿手动修改本文件\n"
        f"\n"
        f"//     当前接口：{source_url}\n"
    )
    return header + json.dumps(cfg, ensure_ascii=False, indent=4) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="在线接口解密 + ZTE 合并")
    ap.add_argument("--url", required=True, help="在线接口地址")
    ap.add_argument("--output", default="NewTV.json", help="输出文件路径")
    ap.add_argument("--zte", default="zte.json", help="ZTE 保留条目定义文件")
    ap.add_argument("--check-only", action="store_true", help="只检查是否有更新，不写文件")
    args = ap.parse_args()

    output = safe_write_path(args.output)
    zte_path = safe_read_path(args.zte)
    zte = json.loads(zte_path.read_text(encoding="utf-8"))

    print(f"[1/3] 下载接口: {args.url}")
    raw = fetch(args.url)
    print(f"      收到 {len(raw)} 字节")

    print("[2/3] 解密配置…")
    remote = extract_config(raw)
    cfg = merge(remote, zte)
    print(f"      站点 {len(cfg['sites'])} 个，直播 {len(cfg['lives'])} 组")

    # 语义对比：注释头时间戳不算变化
    unchanged = output.is_file() and json.loads(strip_comments(output.read_text(encoding="utf-8"))) == cfg

    if unchanged:
        print("[3/3] 配置无实质变化，跳过更新")
        return 0

    print("[3/3] 检测到配置有变化")
    if args.check_only:
        return 0
    output.write_text(render(cfg, args.url), encoding="utf-8")
    print(f"      已写入 {output}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

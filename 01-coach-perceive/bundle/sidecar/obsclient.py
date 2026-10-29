# -*- coding: utf-8 -*-
"""obs-websocket v5 的最小客户端（纯标准库，不依赖 websocket 包）。

只做三件事：握手 / 鉴权 / 发一个请求拿 responseData。
OBS 侧准备：工具 → WebSocket 服务器设置 → 勾选启用、记下端口(默认 4455)与密码。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import uuid

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WsError(RuntimeError):
    pass


class WebSocket:
    """RFC6455 客户端最小实现：文本帧 + ping/pong + close。"""

    def __init__(self, host: str, port: int, path: str = "/", timeout: float = 5.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("WebSocket 握手时连接被关闭（端口不对或不是 obs-websocket？）")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        text = head.decode("latin-1")
        if " 101 " not in text.split("\r\n")[0]:
            raise WsError("WebSocket 握手失败：" + text.split("\r\n")[0])
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept not in text:
            raise WsError("Sec-WebSocket-Accept 不匹配")

    # ---- 帧 ----
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self) -> str | None:
        payload = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(ln)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if op == 0x8:  # close
                return None
            if op in (0x0, 0x1, 0x2):
                payload += data
                if fin:
                    return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class ObsError(RuntimeError):
    pass


class ObsClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str | None = None, timeout: float = 5.0) -> None:
        self.host = host
        self.port = int(port)
        self.password = (password or "").strip() or None
        self.timeout = timeout
        self.ws: WebSocket | None = None

    def connect(self) -> "ObsClient":
        try:
            self.ws = WebSocket(self.host, self.port, timeout=self.timeout)
        except OSError as exc:
            raise ObsError(f"连不上 OBS WebSocket {self.host}:{self.port}（OBS 没开或没启用 WebSocket）：{exc}") from exc
        hello = self._recv_json()
        if hello.get("op") != 0:
            raise ObsError("OBS 首包不是 Hello：" + json.dumps(hello)[:200])
        d = hello.get("d") or {}
        rpc = d.get("rpcVersion") or 1
        ident: dict = {"rpcVersion": 1}
        auth = d.get("authentication")
        if auth:
            if not self.password:
                raise ObsError("OBS 要求密码：请在插件配置里填 obsPassword（或环境变量 DSH_OBS_PASSWORD）")
            secret = base64.b64encode(
                hashlib.sha256((self.password + str(auth.get("salt") or "")).encode()).digest()
            ).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + str(auth.get("challenge") or "")).encode()).digest()
            ).decode()
        self.ws.send_text(json.dumps({"op": 1, "d": ident}))
        msg = self._recv_json()
        if msg.get("op") != 2:
            raise ObsError("OBS 拒绝 Identify（密码错？）：" + json.dumps(msg)[:200])
        self.rpc = msg.get("d", {}).get("negotiatedRpcVersion", rpc)
        return self

    def _recv_json(self) -> dict:
        assert self.ws is not None
        line = self.ws.recv_message()
        if line is None:
            raise ObsError("OBS 关闭了 WebSocket 连接")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise ObsError("OBS 消息不是 JSON：" + line[:200]) from exc

    def request(self, request_type: str, request_data: dict | None = None, timeout: float | None = None) -> dict:
        if self.ws is None:
            self.connect()
        assert self.ws is not None
        rid = uuid.uuid4().hex
        self.ws.send_text(
            json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": rid, "requestData": request_data or {}}})
        )
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            msg = self._recv_json()
            op = msg.get("op")
            if op == 7 and (msg.get("d") or {}).get("requestId") == rid:
                d = msg.get("d") or {}
                status = d.get("requestStatus") or {}
                if not status.get("result"):
                    raise ObsError(f"{request_type} 失败：{status.get('code')} {status.get('comment')}")
                return d.get("responseData") or {}
            # 其它事件（场景切换等）直接丢弃
        raise ObsError(request_type + " 超时")

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None


CAPTURE_KINDS = ("window_capture", "game_capture", "monitor_capture", "display_capture")


def list_capture_inputs(cli: ObsClient) -> list[dict]:
    data = cli.request("GetInputList")
    rows = data.get("inputs") or []
    out = []
    for row in rows:
        kind = row.get("unversionedInputKind") or row.get("inputKind") or ""
        if kind in CAPTURE_KINDS:
            out.append({"name": row.get("inputName"), "kind": kind})
    return out


def window_spec_of(cli: ObsClient, input_name: str) -> dict:
    """window_capture/game_capture 的 inputSettings 里 'window' 形如 '标题:类:程序'。"""
    data = cli.request("GetInputSettings", {"inputName": input_name})
    settings = data.get("inputSettings") or {}
    spec = settings.get("window") or ""
    parts = str(spec).split(":")
    return {
        "kind": data.get("inputKind") or "",
        "windowSpec": spec,
        "title": parts[0] if parts else "",
        "class": parts[1] if len(parts) > 1 else "",
        "exe": parts[2] if len(parts) > 2 else "",
        "monitor": settings.get("monitor"),
        "settings": {k: v for k, v in settings.items() if k in ("window", "monitor", "capture_cursor", "method", "priority")},
    }

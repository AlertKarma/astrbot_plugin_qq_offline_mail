"""端到端验证 SMTP 发送路径：起一个本地极简 SMTP 服务器，真实走一遍
_smtp_send_sync()（security=none 模式），确认邮件报文、收件人、编码都对。

运行：python tests/test_smtp_send.py
"""

from __future__ import annotations

import base64
import email
import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_plugin_logic import _build_stub  # noqa: E402

_build_stub()

import main as plugin_main  # noqa: E402
from email.message import EmailMessage  # noqa: E402
from email.utils import formatdate, make_msgid  # noqa: E402


class MiniSMTPServer:
    """只实现足够 smtplib 走完一次发送的最小 SMTP 协议。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.mail_from = None
        self.rcpt_to = []
        self.data = b""
        self.auth_user = None
        self.auth_pass = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.settimeout(20)
        f = conn.makefile("rwb")

        def send(line):
            f.write((line + "\r\n").encode())
            f.flush()

        send("220 mini-smtp ready")
        in_data = False
        auth_stage = None
        while True:
            raw = f.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")

            if in_data:
                if line == ".":
                    in_data = False
                    send("250 OK queued")
                else:
                    self.data += raw
                continue

            upper = line.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                send("250-mini")
                send("250 AUTH LOGIN")
            elif upper.startswith("AUTH LOGIN"):
                rest = line[len("AUTH LOGIN"):].strip()
                if rest:
                    self.auth_user = base64.b64decode(rest).decode()
                    auth_stage = "pass"
                    send("334 UGFzc3dvcmQ6")
                else:
                    auth_stage = "user"
                    send("334 VXNlcm5hbWU6")
            elif auth_stage == "user":
                self.auth_user = base64.b64decode(line).decode()
                auth_stage = "pass"
                send("334 UGFzc3dvcmQ6")
            elif auth_stage == "pass":
                self.auth_pass = base64.b64decode(line).decode()
                auth_stage = None
                send("235 Authentication successful")
            elif upper.startswith("MAIL FROM:"):
                self.mail_from = line[10:].strip()
                send("250 OK")
            elif upper.startswith("RCPT TO:"):
                self.rcpt_to.append(line[8:].strip())
                send("250 OK")
            elif upper == "DATA":
                in_data = True
                send("354 End data with <CR><LF>.<CR><LF>")
            elif upper == "QUIT":
                send("221 Bye")
                break
            else:
                send("250 OK")

        f.close()
        conn.close()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  -> {extra}" if extra and not cond else ""))
    if not cond:
        check.failed += 1


check.failed = 0


def main():
    print("[SMTP 端到端] security=none，走真实 smtplib 发送路径")
    server = MiniSMTPServer()

    msg = EmailMessage()
    msg["Subject"] = "【AstrBot 告警】QQ 机器人掉线了（napcat-1）"
    msg["From"] = "bot@qq.com"
    msg["To"] = "me@example.com, other@example.com"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(
        "平台实例：napcat-1\n失败原因：反向 WebSocket 已断开\n", charset="utf-8"
    )

    plugin_main.QqOfflineMailPlugin._smtp_send_sync(
        msg,
        host="127.0.0.1",
        port=server.port,
        security="none",
        user="bot@qq.com",
        password="my-auth-code",
    )

    server._thread.join(timeout=20)

    check("服务器收到了 MAIL FROM", server.mail_from is not None, server.mail_from)
    check("MAIL FROM 是发件人", "bot@qq.com" in (server.mail_from or ""), server.mail_from)
    check("收到了 2 个收件人", len(server.rcpt_to) == 2, server.rcpt_to)
    check("收件人 1 正确", "me@example.com" in server.rcpt_to[0], server.rcpt_to)
    check("收件人 2 正确", "other@example.com" in server.rcpt_to[1], server.rcpt_to)
    check("AUTH 用户名正确", server.auth_user == "bot@qq.com", server.auth_user)
    check("AUTH 密码正确", server.auth_pass == "my-auth-code", server.auth_pass)

    raw = server.data
    check("数据段非空", len(raw) > 0, len(raw))

    parsed = email.message_from_bytes(raw)
    check("含 Subject 头", parsed["Subject"] is not None)
    check("含 To 头", parsed["To"] is not None)

    subject = str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
    check("标题中文完整", "QQ 机器人掉线了" in subject, subject)

    payload = parsed.get_payload(decode=True)
    text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    check("正文是 UTF-8", isinstance(payload, bytes), type(payload))
    check("正文中文完整", "反向 WebSocket 已断开" in text, text)
    check("正文含实例 ID", "napcat-1" in text, text)

    server.close()

    print(f"\n==== 失败数：{check.failed} ====")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())

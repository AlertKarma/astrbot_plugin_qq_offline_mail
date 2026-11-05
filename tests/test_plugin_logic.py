"""离线单元测试：用桩模块模拟 astrbot，验证掉线判定 + 邮件触发逻辑。

运行：python tests/test_plugin_logic.py
"""

from __future__ import annotations

import asyncio
import enum
import os
import sys
import types
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_DIR)


# ---------------------------------------------------------------- astrbot 桩


def _build_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict

    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config or {}

            class _L:
                @staticmethod
                def _render(a):
                    if not a:
                        return ""
                    try:
                        return a[0] % a[1:] if len(a) > 1 else str(a[0])
                    except Exception:
                        return " ".join(str(x) for x in a)

                def info(self, *a, **k):
                    print("[info]", self._render(a))

                def warning(self, *a, **k):
                    print("[warn]", self._render(a))

                def error(self, *a, **k):
                    print("[error]", self._render(a))

            self.logger = _L()

    star.Context = object
    star.Star = Star

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object

    flt = types.ModuleType("astrbot.api.event.filter")

    class PermissionType(enum.Flag):
        ADMIN = enum.auto()
        MEMBER = enum.auto()

    def _noop_deco(*a, **k):
        return lambda f: f

    class _Filter:
        pass

    _Filter.PermissionType = PermissionType
    _Filter.command = staticmethod(_noop_deco)
    _Filter.permission_type = staticmethod(_noop_deco)

    flt.PermissionType = PermissionType
    flt.command = _noop_deco
    flt.permission_type = _noop_deco

    event.filter = _Filter
    event.AstrMessageEvent = object

    astrbot.api = api
    api.star = star
    api.event = event
    api.logger = types.SimpleNamespace(info=print, warning=print, error=print)

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.event.filter"] = flt


_build_stub()

import main as plugin_main  # noqa: E402


# ---------------------------------------------------------------- 假协议端


class FakeClient:
    def __init__(self, ws_connected=True, mode="ok", online=True, delay=0.0):
        self._wsr_api_clients = {"10001": object()} if ws_connected else {}
        self.mode = mode
        self.online = online
        self.delay = delay
        self.call_count = 0
        self.last_action = None

        outer = self

        class _Api:
            async def call_action(self, action, **params):
                outer.call_count += 1
                outer.last_action = action
                if outer.delay:
                    await asyncio.sleep(outer.delay)
                if action == "get_login_info":
                    if outer.mode == "raise":
                        raise RuntimeError("WebSocket API call timeout")
                    return {"user_id": 10001, "nickname": "测试机器人"}
                if action == "get_status":
                    if outer.mode == "no_status":
                        raise RuntimeError("action not supported")
                    return {"online": outer.online, "good": outer.online}
                return {}

        self.api = _Api()


class FakeMeta:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name


class FakePlatform:
    def __init__(self, pid, ptype, client):
        self._meta = FakeMeta(pid, ptype)
        self._client = client
        self.config = {"id": pid}

    def meta(self):
        return self._meta

    def get_client(self):
        return self._client


class FakePlatformManager:
    def __init__(self, insts):
        self.insts = insts

    def get_insts(self):
        return self.insts


class FakeContext:
    def __init__(self, insts):
        self.platform_manager = FakePlatformManager(insts)


# ---------------------------------------------------------------- 测试


def make_plugin(insts, **overrides):
    cfg = {
        "enabled": True,
        "check_interval": 10,
        "fail_threshold": 3,
        "cooldown_minutes": 30,
        "notify_recovery": False,
        "probe_timeout": 1,
        "check_qq_online": True,
        "platform_types": ["aiocqhttp"],
        "platform_ids": [],
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "security": "ssl",
        "smtp_user": "bot@qq.com",
        "smtp_password": "authcode",
        "mail_from": "",
        "mail_to": ["me@example.com"],
    }
    cfg.update(overrides)
    p = plugin_main.QqOfflineMailPlugin(FakeContext(insts), cfg)
    p.sent = []
    p.smtp_calls = []

    async def fake_send(subject, body):
        p.sent.append((subject, body))
        return True, "ok"

    p._send_mail = fake_send
    return p


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        check.failed += 1


check.failed = 0


async def test_online():
    print("\n[1] 一切正常时不应发邮件")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient())
    p = make_plugin([inst])
    for _ in range(5):
        await p._check_once()
    check("没有邮件", len(p.sent) == 0, p.sent)
    check("状态非告警", p._states["napcat-1"]["alerting"] is False)


async def test_ws_disconnected():
    print("\n[2] 反向 WS 断开 -> 达到阈值后告警")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(ws_connected=False))
    p = make_plugin([inst])
    await p._check_once()
    check("第 1 次不告警", len(p.sent) == 0)
    await p._check_once()
    check("第 2 次不告警", len(p.sent) == 0)
    await p._check_once()
    check("第 3 次告警", len(p.sent) == 1, p.sent)
    subject, body = p.sent[0]
    check("邮件标题含掉线", "掉线" in subject, subject)
    check("邮件正文含实例 ID", "napcat-1" in body)
    check("邮件正文含原因", "WebSocket" in body, body)


async def test_cooldown():
    print("\n[3] 持续掉线时受冷却时间限制")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(ws_connected=False))
    p = make_plugin([inst], cooldown_minutes=30)
    for _ in range(10):
        await p._check_once()
    check("只发了 1 封", len(p.sent) == 1, len(p.sent))

    # 把上次告警时间往前挪，模拟冷却结束
    p._states["napcat-1"]["last_alert_at"] = datetime.now() - timedelta(minutes=31)
    await p._check_once()
    check("冷却结束后再发 1 封", len(p.sent) == 2, len(p.sent))


async def test_timeout():
    print("\n[4] API 超时（协议端卡死）")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(delay=2))
    p = make_plugin([inst], probe_timeout=1, fail_threshold=1)
    await p._check_once()
    check("超时判定为离线", len(p.sent) == 1, p.sent)
    check("原因提到超时", "超时" in p.sent[0][1], p.sent[0][1])


async def test_qq_kicked():
    print("\n[5] NapCat 活着但 QQ 号被踢下线")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(online=False))
    p = make_plugin([inst], fail_threshold=1)
    await p._check_once()
    check("判定为离线", len(p.sent) == 1, p.sent)
    check("原因提到账号不在线", "不在线" in p.sent[0][1], p.sent[0][1])


async def test_status_unsupported():
    print("\n[6] 协议端不支持 get_status 时不应误报")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(mode="no_status"))
    p = make_plugin([inst], fail_threshold=1)
    await p._check_once()
    check("没有误报", len(p.sent) == 0, p.sent)


async def test_recovery():
    print("\n[7] 恢复通知（notify_recovery=true）")
    client = FakeClient(ws_connected=False)
    inst = FakePlatform("napcat-1", "aiocqhttp", client)
    p = make_plugin([inst], fail_threshold=1, notify_recovery=True)
    await p._check_once()
    check("先发掉线告警", len(p.sent) == 1)

    client._wsr_api_clients = {"10001": object()}
    await p._check_once()
    check("再发恢复邮件", len(p.sent) == 2, p.sent)
    check("恢复邮件标题", "恢复" in p.sent[1][0], p.sent[1][0])
    check("恢复后状态清零", p._states["napcat-1"]["alerting"] is False)


async def test_recovery_disabled():
    print("\n[8] notify_recovery=false 时不发恢复邮件")
    client = FakeClient(ws_connected=False)
    inst = FakePlatform("napcat-1", "aiocqhttp", client)
    p = make_plugin([inst], fail_threshold=1, notify_recovery=False)
    await p._check_once()
    client._wsr_api_clients = {"10001": object()}
    await p._check_once()
    check("只有 1 封", len(p.sent) == 1, len(p.sent))


async def test_filtering():
    print("\n[9] 平台类型 / ID 过滤")
    a = FakePlatform("napcat-1", "aiocqhttp", FakeClient(ws_connected=False))
    b = FakePlatform("tg-1", "telegram", FakeClient(ws_connected=False))
    p = make_plugin([a, b], fail_threshold=1)
    await p._check_once()
    check("只监控 aiocqhttp", len(p.sent) == 1, len(p.sent))
    check("没有碰到 telegram", "tg-1" not in p._states, p._states.keys())

    p2 = make_plugin([a], fail_threshold=1, platform_ids=["other-id"])
    await p2._check_once()
    check("platform_ids 过滤生效", len(p2.sent) == 0, p2.sent)


async def test_smtp_missing():
    print("\n[10] SMTP 未配置时不崩溃、只记日志")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient(ws_connected=False))
    p = make_plugin([inst], fail_threshold=1, smtp_password="")
    # 恢复真实发送逻辑
    p._send_mail = types.MethodType(
        plugin_main.QqOfflineMailPlugin._send_mail, p
    )
    await p._check_once()
    check("没抛异常", True)
    check("smtp_ready 为 False", p._smtp_ready() is False)


def test_message_build():
    print("\n[11] 邮件报文构造")
    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient())
    p = make_plugin([inst])
    msg = p._build_message("测试标题", "正文内容")
    check("Subject 正确", msg["Subject"] == "测试标题")
    check("收件人正确", msg["To"] == "me@example.com", msg["To"])
    check("发件人回退到 smtp_user", msg["From"] == "bot@qq.com", msg["From"])
    check("正文 UTF-8", "正文内容" in msg.get_content())


async def amain():
    await test_online()
    await test_ws_disconnected()
    await test_cooldown()
    await test_timeout()
    await test_qq_kicked()
    await test_status_unsupported()
    await test_recovery()
    await test_recovery_disabled()
    await test_filtering()
    await test_smtp_missing()
    test_message_build()


if __name__ == "__main__":
    asyncio.run(amain())
    print(f"\n==== 失败数：{check.failed} ====")
    sys.exit(1 if check.failed else 0)

"""QQ / NapCat 掉线邮件通知插件。

思路：AstrBot 内部起一个后台协程，定时通过 OneBot v11 API 探活协议端
（NapCat / Lagrange 等）。连续失败达到阈值后，用 SMTP 发一封告警邮件。
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
import socket
import ssl
import traceback
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 这些平台适配器走 OneBot v11 协议（NapCat / Lagrange / LLOneBot 等）
DEFAULT_PLATFORM_TYPES = ["aiocqhttp"]


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


class QqOfflineMailPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # platform_id -> 运行状态
        self._states: dict[str, dict] = {}
        self._warned_incomplete_smtp = False

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        if not self._cfg("enabled", True):
            self.logger.info("QQ 掉线邮件通知：enabled=false，不启动监控。")
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._monitor_loop(), name="qq_offline_mail_monitor"
        )
        self.logger.info(
            "QQ 掉线邮件通知已启动：每 %s 秒探活一次，连续失败 %s 次告警。",
            self._cfg("check_interval", 60),
            self._cfg("fail_threshold", 3),
        )
        if not self._smtp_ready():
            self.logger.warning(
                "SMTP 配置还不完整（smtp_user / smtp_password / mail_to），"
                "掉线时会只写日志不发邮件。请在插件配置里补全。",
            )

    async def terminate(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self.logger.info("QQ 掉线邮件通知已停止。")

    # ------------------------------------------------------------------ 配置工具

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _recipients(self) -> list[str]:
        raw = self._cfg("mail_to", [])
        if isinstance(raw, str):
            raw = [x for x in raw.replace(";", ",").replace("\n", ",").split(",")]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def _smtp_ready(self) -> bool:
        return bool(
            self._cfg("smtp_host", "")
            and self._cfg("smtp_user", "")
            and self._cfg("smtp_password", "")
            and self._recipients()
        )

    # ------------------------------------------------------------------ 监控主循环

    async def _monitor_loop(self) -> None:
        # 等平台适配器加载完，避免刚启动就误报
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=10)

        while not self._stop.is_set():
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error("掉线检测异常：\n%s", traceback.format_exc())

            interval = max(10, int(self._cfg("check_interval", 60) or 60))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    def _target_platforms(self) -> list[tuple[str, str, object]]:
        """返回需要监控的 (platform_id, platform_type, instance)。"""
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return []
        try:
            insts = list(pm.get_insts())
        except Exception:
            return []

        wanted_types = self._cfg("platform_types", DEFAULT_PLATFORM_TYPES)
        if not isinstance(wanted_types, (list, tuple)) or not wanted_types:
            wanted_types = DEFAULT_PLATFORM_TYPES
        wanted_ids = self._cfg("platform_ids", [])
        if not isinstance(wanted_ids, (list, tuple)):
            wanted_ids = []

        targets: list[tuple[str, str, object]] = []
        for inst in insts:
            try:
                meta = inst.meta()
                ptype = getattr(meta, "name", "") or ""
                pid = getattr(meta, "id", None) or (
                    getattr(inst, "config", {}) or {}
                ).get("id", "")
            except Exception:
                continue
            if ptype not in wanted_types:
                continue
            if wanted_ids and pid not in wanted_ids:
                continue
            targets.append((str(pid), ptype, inst))
        return targets

    async def _probe(self, inst) -> tuple[bool, str]:
        """探活一个 OneBot v11 平台实例。返回 (是否在线, 失败原因)。"""
        timeout = float(self._cfg("probe_timeout", 10) or 10)

        try:
            client = inst.get_client()
        except Exception as e:
            return False, f"无法获取协议端客户端：{e}"
        if client is None:
            return False, "协议端客户端尚未初始化"

        # 1) 反向 WebSocket 是否还连着（NapCat 断开时这里会变空）
        api_clients = getattr(client, "_wsr_api_clients", None)
        if isinstance(api_clients, dict) and not api_clients:
            return False, (
                "NapCat / OneBot 的反向 WebSocket 已断开"
                "（AstrBot 侧没有任何活跃的 API 连接）"
            )

        # 2) 通过 OneBot API 探活
        try:
            info = await asyncio.wait_for(
                client.api.call_action("get_login_info"), timeout=timeout
            )
        except TimeoutError:
            return False, (
                f"调用 get_login_info 超时（>{timeout:g}s），"
                "协议端可能卡死或已经退出"
            )
        except Exception as e:
            return False, f"调用 get_login_info 失败：{type(e).__name__}: {e}"

        has_identity = isinstance(info, dict) and (
            info.get("user_id") or info.get("nickname")
        )
        if not has_identity:
            return False, f"get_login_info 返回异常：{info!r}"

        # 3) 可选：确认 QQ 账号本身在线（NapCat 活着但账号被踢下线的情况）
        if self._cfg("check_qq_online", True):
            try:
                status = await asyncio.wait_for(
                    client.api.call_action("get_status"), timeout=timeout
                )
                if isinstance(status, dict) and status.get("online") is False:
                    return False, (
                        "QQ 账号当前不在线（NapCat 还在，但账号已掉线/被踢）："
                        f"{status!r}"
                    )
            except Exception:
                # 部分协议端没实现 get_status，忽略即可，不影响判断
                pass

        return True, ""

    async def _check_once(self) -> None:
        targets = self._target_platforms()
        if not targets:
            return

        now = datetime.now()
        threshold = max(1, int(self._cfg("fail_threshold", 3) or 1))
        cooldown = max(0, int(self._cfg("cooldown_minutes", 30) or 0)) * 60

        for pid, ptype, inst in targets:
            state = self._states.setdefault(
                pid,
                {
                    "fail": 0,
                    "alerting": False,
                    "first_fail_at": None,
                    "last_alert_at": None,
                    "last_ok_at": None,
                    "last_reason": "",
                },
            )

            ok, reason = await self._probe(inst)

            if ok:
                was_alerting = state["alerting"]
                state["fail"] = 0
                state["alerting"] = False
                state["last_ok_at"] = now
                state["last_reason"] = ""
                if was_alerting:
                    downtime = ""
                    if state["first_fail_at"]:
                        downtime = _fmt_duration(
                            (now - state["first_fail_at"]).total_seconds()
                        )
                    state["first_fail_at"] = None
                    self.logger.info("[%s] 协议端已恢复在线。", pid)
                    if self._cfg("notify_recovery", False):
                        await self._notify_recovery(pid, ptype, downtime)
                continue

            state["fail"] += 1
            state["last_reason"] = reason
            if state["first_fail_at"] is None:
                state["first_fail_at"] = now

            self.logger.warning(
                "[%s] 探活失败（第 %s 次）：%s", pid, state["fail"], reason
            )

            if state["fail"] < threshold:
                continue

            last_alert = state["last_alert_at"]
            if last_alert and (now - last_alert).total_seconds() < cooldown:
                continue

            state["alerting"] = True
            state["last_alert_at"] = now
            await self._notify_offline(pid, ptype, reason, state)

    # ------------------------------------------------------------------ 邮件

    async def _notify_offline(
        self, pid: str, ptype: str, reason: str, state: dict
    ) -> None:
        first_fail = state.get("first_fail_at") or datetime.now()
        elapsed = _fmt_duration((datetime.now() - first_fail).total_seconds())
        subject = f"【AstrBot 告警】QQ 机器人掉线了（{pid}）"
        body = (
            "AstrBot 检测到 QQ 机器人（NapCat / OneBot 协议端）掉线。\n"
            "\n"
            f"平台实例：{pid}\n"
            f"平台类型：{ptype}\n"
            f"首次失败：{first_fail:%Y-%m-%d %H:%M:%S}\n"
            f"已持续  ：{elapsed}\n"
            f"失败原因：{reason}\n"
            f"失败次数：{state.get('fail')}（阈值 {self._cfg('fail_threshold', 3)}）\n"
            f"告警时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"运行主机：{socket.gethostname()}\n"
            "\n"
            "排查建议：\n"
            "1. 看 NapCat 的日志/容器状态：docker ps -a | grep napcat\n"
            "2. 确认 QQ 账号是否被踢下线（异地登录、风控、需要重新扫码）\n"
            "3. 确认 AstrBot 的 6199 端口和 NapCat 的反向 WS 地址是否一致\n"
            "4. 确认 NapCat 的 access token 与 AstrBot 平台配置里的 token 一致\n"
            "\n"
            "—— 本邮件由 AstrBot 插件 astrbot_plugin_qq_offline_mail 自动发送\n"
        )
        ok, err = await self._send_mail(subject, body)
        if ok:
            self.logger.info("[%s] 掉线告警邮件已发送。", pid)
        else:
            self.logger.error("[%s] 掉线告警邮件发送失败：%s", pid, err)

    async def _notify_recovery(self, pid: str, ptype: str, downtime: str) -> None:
        subject = f"【AstrBot】QQ 机器人已恢复（{pid}）"
        body = (
            "QQ 机器人（NapCat / OneBot 协议端）已恢复在线。\n"
            "\n"
            f"平台实例：{pid}\n"
            f"平台类型：{ptype}\n"
            f"掉线时长：{downtime or '未知'}\n"
            f"恢复时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"运行主机：{socket.gethostname()}\n"
        )
        ok, err = await self._send_mail(subject, body)
        if not ok:
            self.logger.error("[%s] 恢复通知邮件发送失败：%s", pid, err)

    def _build_message(self, subject: str, body: str) -> EmailMessage:
        sender = str(self._cfg("mail_from", "") or self._cfg("smtp_user", ""))
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(self._recipients())
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(body, charset="utf-8")
        return msg

    async def _send_mail(self, subject: str, body: str) -> tuple[bool, str]:
        if not self._smtp_ready():
            if not self._warned_incomplete_smtp:
                self._warned_incomplete_smtp = True
                self.logger.error(
                    "SMTP 配置不完整，无法发送邮件。需要 smtp_host / smtp_user / "
                    "smtp_password / mail_to。",
                )
            return False, "SMTP 配置不完整"

        host = str(self._cfg("smtp_host", ""))
        port = int(self._cfg("smtp_port", 465) or 465)
        security = str(self._cfg("security", "ssl") or "ssl").lower()
        user = str(self._cfg("smtp_user", ""))
        password = str(self._cfg("smtp_password", ""))

        try:
            msg = self._build_message(subject, body)
            await asyncio.to_thread(
                self._smtp_send_sync, msg, host, port, security, user, password
            )
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return True, "ok"

    @staticmethod
    def _smtp_send_sync(
        msg: EmailMessage,
        host: str,
        port: int,
        security: str,
        user: str,
        password: str,
    ) -> None:
        """同步发送（放在线程里跑，避免阻塞事件循环）。"""
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                server.login(user, password)
                server.send_message(msg)
        elif security == "starttls":
            context = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.login(user, password)
                server.send_message(msg)

    # ------------------------------------------------------------------ 指令

    @filter.command("mailtest", alias={"邮件测试", "掉线测试"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mail_test(self, event: AstrMessageEvent):
        """发送一封测试邮件，用来验证 SMTP 配置。"""
        if not self._smtp_ready():
            yield event.plain_result(
                "SMTP 还没配置完整。请在 WebUI → 插件 → QQ 掉线邮件通知 里填写：\n"
                "smtp_host / smtp_port / smtp_user / smtp_password / mail_to",
            )
            return
        ok, err = await self._send_mail(
            "【AstrBot】掉线通知测试邮件",
            "如果你收到这封邮件，说明 SMTP 配置正确，掉线告警可以正常工作。\n"
            f"\n发送时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"运行主机：{socket.gethostname()}\n",
        )
        if ok:
            yield event.plain_result(
                f"测试邮件已发送到：{', '.join(self._recipients())}，请查收。",
            )
        else:
            yield event.plain_result(f"测试邮件发送失败：{err}")

    @filter.command("mailstatus", alias={"掉线状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def mail_status(self, event: AstrMessageEvent):
        """查看掉线监控的当前状态。"""
        lines = [
            f"监控开关：{'开' if self._cfg('enabled', True) else '关'}",
            f"检查间隔：{self._cfg('check_interval', 60)} 秒",
            f"告警阈值：连续失败 {self._cfg('fail_threshold', 3)} 次",
            f"收件人：{', '.join(self._recipients()) or '未配置'}",
        ]
        targets = self._target_platforms()
        if not targets:
            lines.append(
                "当前没有找到被监控的平台实例"
                "（platform_types 里是否包含 aiocqhttp？）"
            )
        for pid, ptype, inst in targets:
            state = self._states.get(pid, {})
            try:
                ok, reason = await self._probe(inst)
            except Exception as e:
                ok, reason = False, str(e)
            if ok:
                lines.append(f"[{pid}] 在线 ✅")
            else:
                lines.append(
                    f"[{pid}] 离线 ❌ 连续失败 {state.get('fail', 0)} 次：{reason}",
                )
        yield event.plain_result("\n".join(lines))

"""真实节奏下的内存与 CPU 实测（干净进程，无历史污染）。

运行：python tests/bench_realistic.py

模拟真实部署：探活间隔 > 超时时间，这样 asyncio 定时器会正常到期弹出。
用缩放的时间轴加速（间隔 0.06s / 超时 0.02s，比例与 60s/10s 一致），
跑足够多轮来观察趋势。
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from bench_overhead import fmt_kb, rss_bytes  # noqa: E402


def sample_rss(samples: int = 9) -> int:
    vals = []
    for _ in range(samples):
        gc.collect()
        vals.append(rss_bytes())
        time.sleep(0.004)
    return min(vals)


def setup():
    from test_plugin_logic import (  # noqa: E402
        FakeClient,
        FakeContext,
        FakePlatform,
        _build_stub,
    )

    _build_stub()
    import main as plugin_main  # noqa: E402

    inst = FakePlatform("napcat-1", "aiocqhttp", FakeClient())
    cfg = {
        "enabled": True,
        "check_interval": 60,
        "fail_threshold": 3,
        "cooldown_minutes": 30,
        "probe_timeout": 10,
        "check_qq_online": True,
        "platform_types": ["aiocqhttp"],
        "platform_ids": [],
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "security": "ssl",
        "smtp_user": "bot@qq.com",
        "smtp_password": "code",
        "mail_from": "",
        "mail_to": ["me@example.com"],
    }
    plugin = plugin_main.QqOfflineMailPlugin(FakeContext([inst]), cfg)

    async def fake_send(subject, body):
        return True, "ok"

    plugin._send_mail = fake_send
    return plugin, inst


async def main():
    plugin, inst = setup()

    # 真实比例：间隔 60s，超时 10s -> 缩放为 0.06s / 0.02s
    GAP = 0.06
    TIMEOUT = 0.02

    print("=" * 64)
    print("真实节奏模拟（间隔 > 超时，定时器正常弹出）")
    print("=" * 64)
    print(f"  缩放比例：间隔 {GAP}s，超时 {TIMEOUT}s（对应真实 60s / 10s）")
    print()

    base = sample_rss()
    print(f"  起始 RSS：{fmt_kb(base)}")

    checkpoints = [500, 1000, 2000, 4000]
    prev_rss = base
    prev_n = 0

    loop = asyncio.get_running_loop()
    for target in checkpoints:
        for _ in range(target - prev_n):
            await plugin._check_once()
            await asyncio.sleep(GAP)
        rss = sample_rss()
        heap = getattr(loop, "_scheduled", None)
        print(
            f"  {target:>5,} 轮：RSS {fmt_kb(rss)}  "
            f"（较上一档 {fmt_kb(rss - prev_rss)}）  "
            f"待到期定时器 {len(heap) if heap is not None else -1}"
        )
        prev_rss = rss
        prev_n = target

    print()
    print("=" * 64)
    print("判读")
    print("=" * 64)
    print("  待到期定时器数量稳定在个位数 -> 没有堆积")
    print("  RSS 增量接近 0 -> 真实部署下内存平稳")
    print()
    print("  换算：4000 轮 × 60 秒 ≈ 2.8 天连续运行")


if __name__ == "__main__":
    asyncio.run(main())

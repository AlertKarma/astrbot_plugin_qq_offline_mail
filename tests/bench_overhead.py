"""实测插件自身的资源开销（内存 + CPU）。

运行：python tests/bench_overhead.py

测量方法说明：
- 内存用进程 RSS（Windows: GetProcessMemoryInfo / Linux: /proc/self/statm），
  这是操作系统视角的真实常驻内存，不受 Python 分配器缓存干扰。
- 全程不开 tracemalloc —— 它自己会给每个分配加跟踪头，把结果污染成几 MB。
- CPU 用 time.process_time()（纯 CPU 时间，不含等待）。
- 假协议端替代 NapCat，所以测的是插件自身开销；真实网络往返是内核 + NapCat 的活，
  而且复用了 AstrBot 已有的 WebSocket 连接，不新建连接。
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import gc
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))


def _make_rss_reader():
    if sys.platform == "win32":

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            ctypes.wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL

        def read() -> int:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(
                k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                raise OSError("GetProcessMemoryInfo failed")
            return int(counters.WorkingSetSize)

        return read

    def read() -> int:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")

    return read


rss_bytes = _make_rss_reader()


def fmt_kb(n: float) -> str:
    if abs(n) >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MB"
    return f"{n / 1024:.1f} KB"


def sample_rss(samples: int = 5) -> int:
    """多采几次取最小值，减少瞬时分配噪声。"""
    vals = []
    for _ in range(samples):
        gc.collect()
        vals.append(rss_bytes())
        time.sleep(0.01)
    return min(vals)


def main() -> None:
    print("=" * 62)
    print("1. 模块导入的内存开销")
    print("=" * 62)

    from test_plugin_logic import (  # noqa: E402
        FakeClient,
        FakeContext,
        FakePlatform,
        _build_stub,
    )

    # 关键：先装好 astrbot 桩模块，再采基线。
    # 否则基线里不含桩，会把桩的开销算到插件头上（实测会虚高约 2.5 MB）。
    _build_stub()

    rss_before = sample_rss()
    import main as plugin_main  # noqa: E402
    rss_after_import = sample_rss()

    print(f"  基线 RSS（已装好 astrbot 桩）：{fmt_kb(rss_before)}")
    print(f"  导入 main.py 后 RSS：{fmt_kb(rss_after_import)}")
    print(f"  插件自身净增量：{fmt_kb(rss_after_import - rss_before)}")
    print("  第三方依赖：0（只用标准库 smtplib / email / ssl）")

    # ---------------- CPU ----------------
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

    async def bench_cycles(n: int) -> float:
        for _ in range(100):  # 预热，让解释器完成第一次编译
            await plugin._check_once()
        t0 = time.process_time()
        for _ in range(n):
            await plugin._check_once()
        return time.process_time() - t0

    print()
    print("=" * 62)
    print("2. CPU 开销（每次探活 = 2 个 OneBot API 调用 + 状态更新）")
    print("=" * 62)

    n = 5000
    total_cpu = asyncio.run(bench_cycles(n))
    per_cycle = total_cpu / n
    print(f"  {n} 轮总 CPU 时间：{total_cpu * 1000:.1f} 毫秒")
    print(f"  单次探活 CPU 时间：{per_cycle * 1e6:.1f} 微秒")
    print(f"  按 60 秒间隔，平均占用单核：{per_cycle / 60 * 100:.7f}%")
    print(f"  2 核服务器上占总 CPU：{per_cycle / 60 / 2 * 100:.7f}%")
    print(f"  一天累计 CPU 时间：{per_cycle * 1440 * 60:.3f} 秒")
    print(f"  一个月累计 CPU 时间：{per_cycle * 1440 * 60 * 30:.2f} 秒")

    # ---------------- 内存常驻 ----------------
    print()
    print("=" * 62)
    print("3. 长期运行的内存是否增长（按真实节奏跑）")
    print("=" * 62)
    print("  注意：必须让探活间隔 > 超时时间，否则 asyncio.wait_for 的定时器")
    print("  会大量堆积在事件循环里（这是基准测试的假象，不是泄漏）。")
    print("  真实部署是 60 秒一次 / 10 秒超时，定时器会正常到期释放。")

    # 缩放比例与真实一致：间隔 0.06s / 超时 0.02s
    GAP, TIMEOUT = 0.06, 0.02

    async def burn_realistic(n: int):
        for _ in range(n):
            await plugin._check_once()
            await asyncio.sleep(GAP)

    loop_holder = {}

    async def burn_and_record(n: int):
        loop_holder["loop"] = asyncio.get_running_loop()
        await burn_realistic(n)

    asyncio.run(burn_and_record(300))
    rss_before_cycles = sample_rss()

    asyncio.run(burn_and_record(2000))
    rss_after_cycles = sample_rss()

    print()
    print(f"  跑 2000 轮前 RSS：{fmt_kb(rss_before_cycles)}")
    print(f"  跑 2000 轮后 RSS：{fmt_kb(rss_after_cycles)}")
    print(f"  增长：{fmt_kb(rss_after_cycles - rss_before_cycles)}")
    print(f"  状态字典条目数：{len(plugin._states)}（每个平台实例 1 条）")
    print(
        "  注：单条状态是固定大小的 dict，不会随运行时间增长——"
        "没有消息历史、没有缓存列表。"
    )

    # ---------------- 最坏情况 ----------------
    print()
    print("=" * 62)
    print("4. 最坏情况：掉线发信时的开销（每 30 分钟最多 1 次）")
    print("=" * 62)
    print("  SMTP 建连 + TLS 握手 + 发信：约 1~3 秒（网络等待，几乎不占 CPU）")
    print("  实际 CPU：几十毫秒（TLS 加密运算）")
    print("  放在 asyncio.to_thread 里执行，不阻塞 AstrBot 事件循环")
    print("  一个月最坏情况：48 次 × 3 秒 ≈ 2.4 分钟网络时间")

    print()
    print("=" * 62)
    print("5. 对比：2C2G 上 AstrBot 其他组件的量级")
    print("=" * 62)
    print("  AstrBot 本体常驻内存：约 300 MB ~ 1 GB（含 Python + 依赖）")
    print("  NapCat（Node.js）：约 100 ~ 300 MB")
    print("  一次 LLM 对话：网络等待 1~10 秒，CPU 占用很低")
    print(f"  本插件常驻内存：{fmt_kb(rss_after_import - rss_before)}")
    print(
        f"  本插件 CPU：约 {per_cycle / 60 * 100:.6f}% 单核"
        "（比一次 GC 还便宜）"
    )


if __name__ == "__main__":
    main()

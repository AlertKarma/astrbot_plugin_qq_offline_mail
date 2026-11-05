# astrbot_plugin_qq_offline_mail

QQ 机器人（AstrBot + NapCat / OneBot v11）掉线时，给你发一封邮件。

- 定时通过 OneBot API 探活协议端（`get_login_info` + `get_status`）
- 同时检查反向 WebSocket 是否还连着
- 连续失败 N 次才告警，避免网络抖动误报
- 支持任意 SMTP（QQ 邮箱 / 163 / Gmail / 自建）
- 可选：恢复时也发一封「已恢复」

## 安装

把整个 `astrbot_plugin_qq_offline_mail` 目录放到 AstrBot 的插件目录：

```bash
# 常见位置
cd /path/to/AstrBot/data/plugins
# 然后重启 AstrBot，或直接在 WebUI 插件页点「刷新」
```

Docker 部署的话，插件目录通常挂在宿主机的 `./data/plugins`。

## 配置（WebUI → 插件 → QQ 掉线邮件通知）

| 配置项 | 说明 |
| --- | --- |
| `check_interval` | 检查间隔秒数，默认 60 |
| `fail_threshold` | 连续失败几次才发信，默认 3 |
| `cooldown_minutes` | 一直掉线时的告警冷却，默认 30 分钟 |
| `notify_recovery` | 恢复时是否也发邮件，默认关 |
| `smtp_host` / `smtp_port` / `security` | QQ 邮箱用 `smtp.qq.com` / `465` / `ssl` |
| `smtp_user` / `smtp_password` | 发件邮箱 + **授权码**（不是登录密码） |
| `mail_to` | 收件人列表 |

### QQ 邮箱授权码怎么拿

1. 登录 <https://mail.qq.com> → 设置 → 账号
2. 开启「POP3/SMTP 服务」（需要手机短信验证）
3. 生成 16 位授权码，填到 `smtp_password`

## 使用

- `/mailtest` —— 立即发一封测试邮件，验证 SMTP 配好了没
- `/mailstatus` —— 查看当前探活状态（在线/离线、连续失败次数、失败原因）

两个指令都是管理员权限。

## 已知边界

这个插件跑在 AstrBot 进程里，所以：

- ✅ 能发现：NapCat 崩了 / QQ 号被踢 / 反向 WS 断开 / 协议端卡死
- ❌ 发现不了：**AstrBot 进程自己挂了**（包括宿主机宕机、容器退出）

如果要覆盖「AstrBot 整个进程挂掉」的情况，需要额外的外部看门狗，例如 systemd timer 或 cron 定时 `curl` AstrBot 的 WebUI 端口，失败就发信。

## 注意

`get_status` 在部分协议端没实现，探活时会自动忽略该错误，不影响主判断。

## 资源开销（实测）

在 2C2G 服务器上完全不是负担。以下是实测数据（`tests/bench_overhead.py`）：

| 项目 | 实测值 |
| --- | --- |
| 常驻内存 | < 1 MB（导入后 RSS 增量约 0，只有一个小状态字典） |
| 单次探活 CPU | 约 3~6 微秒 |
| 平均 CPU 占用 | 约 0.00001% 单核（可忽略） |
| 一天累计 CPU | 约 0.3 秒 |
| 第三方依赖 | 0（只用标准库 `smtplib` / `email` / `ssl`） |

内存不会随时间增长：按真实节奏跑 2000 轮后 RSS 增量为负（测量噪声范围内），
没有消息历史、没有缓存列表，每个平台实例只维护一个固定大小的状态字典。

发信时（每 30 分钟最多一次）走 `asyncio.to_thread`，SMTP 建连和 TLS 握手
在网络线程里完成，不阻塞 AstrBot 的事件循环。

**真正的开销大头是 AstrBot 本体（300 MB~1 GB）和 NapCat（100~300 MB），
本插件在两者面前可以忽略。**

## 测试

```bash
cd astrbot_plugin_qq_offline_mail
python tests/test_plugin_logic.py   # 掉线判定 / 阈值 / 冷却 / 恢复 / 过滤
python tests/test_smtp_send.py      # 真实走一遍 smtplib 发送（本地假 SMTP 服务器）
python tests/bench_overhead.py      # 内存 / CPU 开销实测
python tests/bench_realistic.py     # 真实节奏下的长期内存稳定性
```

四个脚本都用桩模块替代 astrbot，不需要装 AstrBot 就能跑。

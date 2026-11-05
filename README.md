# QQ 掉线邮件通知

> 给 AstrBot 用的插件：QQ 机器人掉线时，发邮件提醒你。

<div align="center">

![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.5-1f6feb?style=flat-square)
![Platform](https://img.shields.io/badge/OneBot-v11-7c3aed?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Deps](https://img.shields.io/badge/dependencies-0-2ea043?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-2ea043?style=flat-square)

</div>

---

NapCat 半夜掉线，你要到第二天才发现？这个插件每隔一分钟探活一次，
连续失败就给你发邮件。

## 特性

|  |  |
| --- | --- |
| 🔍 **三重探活** | 反向 WebSocket 连接状态 + `get_login_info` + `get_status` |
| 🎯 **不误报** | 连续失败 N 次才告警，扛得住网络抖动 |
| 🔕 **不刷屏** | 告警冷却期内不重复发信 |
| 📮 **通用 SMTP** | QQ 邮箱 / 163 / Gmail / 自建服务端都行 |
| 🪶 **几乎零开销** | 无第三方依赖，常驻内存 < 1 MB |
| 🛠️ **可自检** | `/mailtest` 验证配置，`/mailstatus` 看实时状态 |

## 能发现什么

- ✅ NapCat 崩溃 / 容器退出
- ✅ QQ 号被踢下线（NapCat 还活着但账号掉了）
- ✅ 反向 WebSocket 断开
- ✅ 协议端卡死无响应

> [!WARNING]
> **发现不了 AstrBot 进程自己挂掉。** 插件跑在 AstrBot 内部，进程没了它也没了。要覆盖这种情况，需要额外的外部看门狗（systemd timer 或 cron 定时 `curl` WebUI 端口）。

## 安装

把整个目录放进 AstrBot 的插件目录，然后重启：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/AlertKarma/astrbot_plugin_qq_offline_mail.git
```

Docker 部署的话，插件目录一般映射在宿主机的 `./data/plugins`。

重启后日志里应该出现：

```
QQ 掉线邮件通知已启动：每 60 秒探活一次，连续失败 3 次告警。
```

## 配置

WebUI → 插件 → **QQ 掉线邮件通知**。

### 必填

| 配置项 | 说明 |
| --- | --- |
| `smtp_user` | 发件邮箱 |
| `smtp_password` | **授权码**，不是登录密码 |
| `mail_to` | 收件人列表，可多个 |

### 常用（有默认值，一般不用改）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `check_interval` | `60` | 检查间隔（秒） |
| `fail_threshold` | `3` | 连续失败几次才告警 |
| `cooldown_minutes` | `30` | 告警冷却（分钟） |
| `notify_recovery` | `false` | 恢复时也发一封 |
| `smtp_host` | `smtp.qq.com` | SMTP 服务器 |
| `smtp_port` | `465` | 端口 |
| `security` | `ssl` | 加密方式 |

<details>
<summary><b>QQ 邮箱授权码怎么拿</b></summary>

1. 登录 [mail.qq.com](https://mail.qq.com) → 设置 → 账号
2. 开启「POP3/SMTP 服务」（要手机短信验证）
3. 生成 16 位授权码，填进 `smtp_password`

> 163 邮箱同理，Gmail 用 `smtp.gmail.com` + `587` + `starttls` + 应用专用密码。

</details>

## 使用

| 指令 | 作用 |
| --- | --- |
| `/mailtest` | 立即发一封测试邮件 |
| `/mailstatus` | 查看探活状态和失败原因 |

两个都是管理员权限。配好后先发 `/mailtest`，确认能收到再等掉线告警。

## 资源开销

实测数据（`tests/bench_overhead.py`），2C2G 机器完全无压力：

| 项目 | 实测值 |
| --- | --- |
| 常驻内存 | < 1 MB |
| 单次探活 CPU | 3～6 微秒 |
| 平均 CPU 占用 | 约 0.00001% 单核 |
| 一天累计 CPU | 约 0.3 秒 |
| 第三方依赖 | 0 |

内存不随时间增长：没有消息历史、没有缓存列表，每个平台实例只维护一个固定大小的状态字典。发信走 `asyncio.to_thread`，SMTP 握手不阻塞事件循环。

作为对比：AstrBot 本体常驻 300 MB 到 1 GB，NapCat 100 到 300 MB。

## 测试

四个离线脚本，用桩模块替代 `astrbot`，**不装 AstrBot 也能跑**：

```bash
python tests/test_plugin_logic.py   # 掉线判定 / 阈值 / 冷却 / 恢复 / 过滤
python tests/test_smtp_send.py      # 真实走一遍 smtplib（本地假 SMTP 服务器）
python tests/bench_overhead.py      # 内存 / CPU 开销
python tests/bench_realistic.py     # 真实节奏下的长期内存稳定性
```

## 已知限制

`get_status` 在部分协议端没实现，探活时会自动跳过，不影响主判断。

## License

[MIT](LICENSE)

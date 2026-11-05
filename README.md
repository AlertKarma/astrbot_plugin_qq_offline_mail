# QQ 掉线邮件通知

AstrBot 插件：QQ 机器人（NapCat / OneBot v11）掉线时，给你发一封邮件。

## 功能

- 定时通过 OneBot API 探活协议端，同时检查反向 WebSocket 连接
- 连续失败若干次才告警，避免网络抖动误报
- 告警后进入冷却期，持续掉线不会反复发信
- 支持 QQ 邮箱、163、Gmail 及自建 SMTP 服务
- 提供 `/mailtest` 和 `/mailstatus` 用于自检

## 安装

把插件目录放到 AstrBot 的 `data/plugins` 下，然后重启：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/AlertKarma/astrbot_plugin_qq_offline_mail.git
```

Docker 部署时，插件目录一般映射在宿主机的 `./data/plugins`。

重启后日志会输出：

```
QQ 掉线邮件通知已启动：每 60 秒探活一次，连续失败 3 次告警。
```

## 配置

在 WebUI 的插件页找到「QQ 掉线邮件通知」，填这三项就能用：

| 配置项 | 说明 |
| --- | --- |
| `smtp_user` | 发件邮箱 |
| `smtp_password` | 邮箱授权码，不是登录密码 |
| `mail_to` | 收件人，可填多个 |

其余配置都有默认值：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `check_interval` | 60 | 检查间隔（秒） |
| `fail_threshold` | 3 | 连续失败几次才告警 |
| `cooldown_minutes` | 30 | 告警冷却（分钟） |
| `notify_recovery` | false | 恢复时是否也发一封 |
| `smtp_host` | smtp.qq.com | SMTP 服务器 |
| `smtp_port` | 465 | 端口 |
| `security` | ssl | 加密方式 |

QQ 邮箱的授权码在邮箱设置的账号页面生成，其他邮箱大同小异。

## 使用

| 指令 | 作用 |
| --- | --- |
| `/mailtest` | 立即发一封测试邮件 |
| `/mailstatus` | 查看当前探活状态 |

两个指令都需要管理员权限。配好后建议先发一次 `/mailtest`，确认能正常收到。

## 说明

插件运行在 AstrBot 进程内，能发现的是协议端的问题：NapCat 崩溃、QQ 号被踢下线、反向 WebSocket 断开、协议端卡死。

如果 AstrBot 进程本身挂了，这个插件也一起没了，自然发不出邮件。要覆盖这种情况，得另外配一个外部看门狗，比如 cron 定时请求 WebUI 端口。

另外，`get_status` 在部分协议端没有实现，探活时会跳过这项检查。

## License

MIT

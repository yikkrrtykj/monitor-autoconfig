# 飞书自建应用群解析排障

自建应用主动发卡片需要先确定目标群。配置填写 `oc_` 开头的 Chat ID 时系统直接使用；填写群名称时，系统调用飞书 `GET /open-apis/im/v1/chats` 分页查找机器人所在群。

## HTTP 400 的含义

旧代码只记录 HTTP 状态，丢掉了飞书响应中的业务错误码，所以日志只能看到 `HTTP 400`。当前版本会同时记录 `code`、`msg` 和针对常见错误的处理提示，并在赛事控制台明确显示实际使用“自建应用”还是“Webhook 回退”。

常见处理：

1. 在飞书开发者后台为应用开启机器人能力。
2. 申请“以应用的身份发消息”权限 `im:message:send_as_bot`；如果配置的是群名称，再申请“查看群信息”权限 `im:chat:read`。发布新版本并由管理员完成审批/安装。直接填写 `oc_` Chat ID 可跳过群列表读取，但不能省略发送权限。
3. 把应用机器人加入目标告警群。
4. 控制台可填写共享群名称；若同名群或群列表权限受限，直接填写 `oc_` 开头的 Chat ID，跳过群列表解析。共享群中的每台 VM 必须配置唯一 `EVENT_NAME`，命令也必须带该赛事名称。
5. 应用配置后点“发送测试告警”。结果必须显示“已通过自建应用发送”；显示“Webhook 回退”只证明兜底通道可用。

官方接口说明：

- [获取群列表](https://open.feishu.cn/document/server-docs/group/chat/list)
- [获取群信息](https://open.feishu.cn/document/server-docs/group/chat/get-2)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)
- [权限列表](https://open.feishu.cn/document/server-docs/application-scope/scope-list?lang=zh-CN)

## 现场验证

```bash
cd ~/monitor-autoconfig/librenms+grafana

docker compose exec -T -w /app alertmanager-feishu-bridge \
  python -c "import urllib.request; r=urllib.request.Request('http://127.0.0.1:5005/test-alert',data=b'{}',headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(r,timeout=15).read().decode())"

docker compose logs --since=5m alertmanager-feishu-bridge feishu-ws | \
  grep -E '\[APP\]|\[TEST\]|chat list|site-group|routing is degraded'
```

也可以直接在赛事控制台点击“发送测试告警”，页面会显示实际使用的通道和自建应用错误原因。

预期 JSON 中 `ok=true`、`channel="app"`、`appChatResolved=true` 且 `appError` 为空。若 `channel="webhook"`，消息虽已收到，但应用通道仍需按返回的业务错误处理。

## 网络巡检分页卡

`INSPECTION_PAGINATION_ENABLED` 默认是 `false`。只有同一飞书 App 确认仅运行一个有效长连接
接收客户端时，才可设置为 `true`。该限制不是群数量限制：同一实例可以服务多个巡检 session，
同群成员也都可以翻同一张只读卡；限制针对共享同一 App 的多个接收进程。

启用后的正常行为：

- 初次网络巡检只回复第一页，每页 6 个完整检查项；
- 单页没有按钮，多页只显示有效的上一页/下一页；
- 翻页更新原卡，不发送新巡检卡，也不重新采集监控数据；
- session 保存于 Bridge 内存，20 分钟到期，Bridge 重启后旧卡提示结果不可用；
- 跨群、错误卡片消息 ID 或缺失回调上下文会被拒绝，原卡保持不变；
- 分页 callback 与 Pending Delete 开关独立，但删除按钮仍只在原开关启用时处理。

常见提示：

| 提示 | 含义 |
|---|---|
| 本次巡检结果已过期，请重新执行巡检。 | 当前进程明确记录该 session 已超过 20 分钟 |
| 本次巡检结果不可用，请重新执行巡检。 | session 未知，常见于 Bridge 重启或 callback 到达错误实例 |
| 无法确认巡检卡片来源。 | 群、App 或卡片消息上下文缺失或不匹配 |

完整部署和手机/桌面验收矩阵见
[`../../docs/runbooks/feishu-ux2-acceptance.md`](../../docs/runbooks/feishu-ux2-acceptance.md)。

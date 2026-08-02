# 飞书自建应用群解析排障

自建应用主动发卡片需要先确定目标群。配置填写 `oc_` 开头的 Chat ID 时系统直接使用；填写群名称时，系统调用飞书 `GET /open-apis/im/v1/chats` 分页查找机器人所在群。

## HTTP 400 的含义

旧代码只记录 HTTP 状态，丢掉了飞书响应中的业务错误码，所以日志只能看到 `HTTP 400`。当前版本会同时记录 `code`、`msg` 和针对常见错误的处理提示，并在赛事控制台明确显示实际使用“自建应用”还是“Webhook 回退”。

常见处理：

1. 在飞书开发者后台为应用开启机器人能力。
2. 申请“以应用的身份发消息”权限 `im:message:send_as_bot`；如果配置的是群名称，再申请“查看群信息”权限 `im:chat:read`。发布新版本并由管理员完成审批/安装。直接填写 `oc_` Chat ID 可跳过群列表读取，但不能省略发送权限。
3. 把应用机器人加入目标告警群。
4. 控制台优先填写唯一群名称；若同名群或群列表权限受限，直接填写 `oc_` 开头的 Chat ID，跳过群列表解析。
5. 应用配置后点“发送测试告警”。结果必须显示“已通过自建应用发送”；显示“Webhook 回退”只证明兜底通道可用。

官方接口说明：

- [获取群列表](https://open.feishu.cn/document/server-docs/group/chat/list)
- [获取群信息](https://open.feishu.cn/document/server-docs/group/chat/get-2)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN)
- [权限列表](https://open.feishu.cn/document/server-docs/application-scope/scope-list?lang=zh-CN)

## 现场验证

```bash
cd ~/monitor-autoconfig/librenms+grafana

docker compose exec -T alertmanager-feishu-bridge \
  python -c "import urllib.request; r=urllib.request.Request('http://127.0.0.1:5005/test-alert',data=b'{}',headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(r,timeout=15).read().decode())"

docker compose logs --since=5m alertmanager-feishu-bridge | \
  grep -E '\[APP\]|\[TEST\]|chat list|interactive card'
```

也可以直接在赛事控制台点击“发送测试告警”，页面会显示实际使用的通道和自建应用错误原因。

预期 JSON 中 `ok=true`、`channel="app"`、`appChatResolved=true` 且 `appError` 为空。若 `channel="webhook"`，消息虽已收到，但应用通道仍需按返回的业务错误处理。

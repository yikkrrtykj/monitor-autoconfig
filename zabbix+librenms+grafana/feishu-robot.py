#!/usr/bin/env python3
# _*_coding:utf-8 _*_

import sys, requests, json

subject = str(sys.argv[1])
message = str(sys.argv[2])
robot_token = str(sys.argv[3])

# 解析告警级别 (Zabbix标准级别)
severity = str(sys.argv[4]) if len(sys.argv) > 4 else "Not classified"

# 检测是否为恢复通知
is_recovery = "恢复" in subject or "恢复" in message or "RESOLVED" in subject.upper() or "OK" in subject.upper()

# 根据Zabbix官方标准设置卡片颜色
severity_colors = {
    "Not classified": "grey",
    "Information": "blue",
    "Warning": "yellow",
    "Average": "orange",
    "High": "red",
    "Disaster": "purple"   
}

# 如果是恢复通知，使用绿色背景
if is_recovery:
    card_color = "green"
else:
    card_color = severity_colors.get(severity, severity_colors["Not classified"])

robot = 'https://open.feishu.cn/open-apis/bot/v2/hook/' + robot_token

data = {
    "msg_type": "interactive",
    "card": {
        "schema": "2.0",
        "config": {
            "style": {
                "text_size": {
                    "normal_v2": {
                        "default": "normal",
                        "pc": "normal",
                        "mobile": "heading"
                    }
                }
            }
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "markdown",
                    "content": message,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px"
                }
            ]
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": subject
            },
            "subtitle": {
                "tag": "plain_text",
                "content": "告警通知"
            },
            "template": card_color,
            "padding": "12px 12px 12px 12px"
        }
    }
}
headers = {
    'Content-Type': 'application/json'
}

response = requests.post(url=robot, data=json.dumps(data), headers=headers)

print(response.json())
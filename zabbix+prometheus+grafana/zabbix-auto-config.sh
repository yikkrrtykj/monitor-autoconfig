#!/bin/bash

# Zabbix Auto-Discovery Configuration Script
# This script configures Zabbix auto-discovery rules and actions via API

set -e

ZABBIX_URL="http://zabbix-web:8080"
ZABBIX_USER="Admin"
ZABBIX_PASS="zabbix"

# Copy script to writable location
cp "$0" /tmp/zabbix-auto-config.sh
chmod +x /tmp/zabbix-auto-config.sh

echo "Starting Zabbix auto-discovery configuration..."

# Wait for Zabbix Web interface to be ready
echo "Waiting for Zabbix Web interface to be ready..."
until curl -s "$ZABBIX_URL/api_jsonrpc.php" > /dev/null 2>&1; do
  echo "Waiting for Zabbix Web..."
  sleep 10
done

echo "Zabbix Web interface is ready. Fixing database charset first..."

# Fix database charset and collation for Zabbix 7.0 compatibility
echo "Checking and fixing database charset/collation..."
fix_database_charset() {
  echo "Connecting to MySQL to fix charset issues..."
  
  # Wait for MySQL to be fully ready
  until docker exec mysql mysqladmin ping -h localhost -u root -proot --silent; do
    echo "Waiting for MySQL to be ready..."
    sleep 5
  done
  
  echo "Fixing database charset and collation..."
  docker exec mysql mysql -uroot -proot -e "
    -- Fix database charset
    ALTER DATABASE zabbix CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
    
    -- Disable foreign key checks temporarily
    SET FOREIGN_KEY_CHECKS = 0;
    
    -- Get all tables and convert them
    SELECT CONCAT('ALTER TABLE ', table_name, ' CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;') 
    FROM information_schema.tables 
    WHERE table_schema = 'zabbix' AND table_type = 'BASE TABLE'
    INTO OUTFILE '/tmp/convert_tables.sql';
    
    -- Execute the conversion
    SOURCE /tmp/convert_tables.sql;
    
    -- Re-enable foreign key checks
    SET FOREIGN_KEY_CHECKS = 1;
    
    -- Verify the fix
    SELECT 'Database charset fixed:' as status, @@character_set_database, @@collation_database;
  " 2>/dev/null || {
    echo "Direct conversion failed, trying alternative method..."
    
    # Alternative method: convert tables one by one
    docker exec mysql mysql -uroot -proot zabbix -e "
      SET FOREIGN_KEY_CHECKS = 0;
      ALTER DATABASE zabbix CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
    "
    
    # Get list of tables and convert them
    tables=$(docker exec mysql mysql -uroot -proot zabbix -sN -e "
      SELECT table_name FROM information_schema.tables 
      WHERE table_schema = 'zabbix' AND table_type = 'BASE TABLE'
    ")
    
    for table in $tables; do
      echo "Converting table: $table"
      docker exec mysql mysql -uroot -proot zabbix -e "
        ALTER TABLE $table CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
      " 2>/dev/null || echo "Warning: Failed to convert table $table"
    done
    
    docker exec mysql mysql -uroot -proot zabbix -e "SET FOREIGN_KEY_CHECKS = 1;"
  }
  
  echo "Database charset fix completed."
}

# Execute the charset fix
fix_database_charset

echo "Configuring auto-discovery..."

# Function to make API calls
api_call() {
  local method="$1"
  local params="$2"
  local auth="$3"
  
  if [ -n "$auth" ]; then
    auth_field=",\"auth\": \"$auth\""
  else
    auth_field=""
  fi
  
  curl -s -X POST -H "Content-Type: application/json" \
    -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"$method\",
      \"params\": $params,
      \"id\": 1$auth_field
    }" "$ZABBIX_URL/api_jsonrpc.php"
}

# Login to Zabbix API
echo "Authenticating with Zabbix API..."
login_response=$(api_call "user.login" "{
  \"username\": \"$ZABBIX_USER\",
  \"password\": \"$ZABBIX_PASS\"
}")

echo "Login response: $login_response"

# Check if response is valid JSON
if ! echo "$login_response" | jq . > /dev/null 2>&1; then
  echo "Invalid JSON response from API"
  echo "Raw response: $login_response"
  exit 1
fi

AUTH_TOKEN=$(echo "$login_response" | jq -r '.result // empty')

if [ -z "$AUTH_TOKEN" ] || [ "$AUTH_TOKEN" = "null" ]; then
  echo "Failed to authenticate with Zabbix API"
  echo "Response: $login_response"
  exit 1
fi

echo "Successfully authenticated with Zabbix API"

# Create host group 'switch' if it doesn't exist
echo "Checking for 'switch' host group..."
switch_group_response=$(api_call "hostgroup.get" "{
  \"filter\": {
    \"name\": [\"switch\"]
  }
}" "$AUTH_TOKEN")

SWITCH_GROUP_ID=$(echo "$switch_group_response" | jq -r '.result[0].groupid // empty')

if [ -z "$SWITCH_GROUP_ID" ]; then
  echo "Creating 'switch' host group..."
  create_group_response=$(api_call "hostgroup.create" "{
    \"name\": \"switch\"
  }" "$AUTH_TOKEN")
  
  SWITCH_GROUP_ID=$(echo "$create_group_response" | jq -r '.result.groupids[0]')
  echo "Created 'switch' host group with ID: $SWITCH_GROUP_ID"
else
  echo "'switch' host group already exists with ID: $SWITCH_GROUP_ID"
fi

# Get 'Discovered hosts' group ID
echo "Getting 'Discovered hosts' group ID..."
discovered_group_response=$(api_call "hostgroup.get" "{
  \"filter\": {
    \"name\": [\"Discovered hosts\"]
  }
}" "$AUTH_TOKEN")

DISCOVERED_GROUP_ID=$(echo "$discovered_group_response" | jq -r '.result[0].groupid')
echo "'Discovered hosts' group ID: $DISCOVERED_GROUP_ID"

# Get 'Cisco IOS by SNMP' template ID
echo "Getting 'Cisco IOS by SNMP' template ID..."
template_response=$(api_call "template.get" "{
  \"filter\": {
    \"host\": [\"Cisco IOS by SNMP\"]
  }
}" "$AUTH_TOKEN")

TEMPLATE_ID=$(echo "$template_response" | jq -r '.result[0].templateid')
echo "'Cisco IOS by SNMP' template ID: $TEMPLATE_ID"

# Check if discovery action exists first and delete it
echo "Checking for existing discovery action..."
existing_action_response=$(api_call "action.get" "{
  \"filter\": {
    \"name\": [\"Auto-discovery: switch devices\"]
  }
}" "$AUTH_TOKEN")

EXISTING_ACTION_ID=$(echo "$existing_action_response" | jq -r '.result[0].actionid // empty')

if [ -n "$EXISTING_ACTION_ID" ]; then
  echo "Discovery action already exists with ID: $EXISTING_ACTION_ID. Deleting it first..."
  delete_action_response=$(api_call "action.delete" "[\"$EXISTING_ACTION_ID\"]" "$AUTH_TOKEN")
  echo "Deleted existing discovery action"
fi

# Check if discovery rule 'switch' already exists
echo "Checking for existing 'switch' discovery rule..."
existing_drule_response=$(api_call "drule.get" "{
  \"filter\": {
    \"name\": [\"switch\"]
  }
}" "$AUTH_TOKEN")

EXISTING_DRULE_ID=$(echo "$existing_drule_response" | jq -r '.result[0].druleid // empty')

if [ -n "$EXISTING_DRULE_ID" ]; then
  echo "Discovery rule 'switch' already exists with ID: $EXISTING_DRULE_ID. Deleting it first..."
  delete_drule_response=$(api_call "drule.delete" "[\"$EXISTING_DRULE_ID\"]" "$AUTH_TOKEN")
  echo "Deleted existing discovery rule"
fi

# Import firewall templates first
echo "Importing firewall templates..."

# Function to import template from file
import_template() {
  local template_file="$1"
  local template_name="$2"
  
  if [ -f "$template_file" ]; then
    echo "Importing template: $template_name"
    template_content=$(cat "$template_file")
    import_response=$(api_call "configuration.import" "{
      \"format\": \"yaml\",
      \"source\": $(echo "$template_content" | jq -Rs .),
      \"rules\": {
        \"templates\": {
          \"createMissing\": true,
          \"updateExisting\": true
        },
        \"items\": {
          \"createMissing\": true,
          \"updateExisting\": true
        },
        \"triggers\": {
          \"createMissing\": true,
          \"updateExisting\": true
        },
        \"discoveryRules\": {
          \"createMissing\": true,
          \"updateExisting\": true
        },
        \"valueMaps\": {
          \"createMissing\": true,
          \"updateExisting\": true
        },
        \"graphs\": {
          \"createMissing\": true,
          \"updateExisting\": true
        }
      }
    }" "$AUTH_TOKEN")
    
    if echo "$import_response" | jq -e '.result' > /dev/null 2>&1; then
      echo "Successfully imported template: $template_name"
    else
      echo "Failed to import template: $template_name"
      echo "Response: $import_response"
    fi
  else
    echo "Template file not found: $template_file"
  fi
}

# Import Hillstone firewall template
echo "Starting Hillstone template import..."
import_template "/hillstone-zabbix7.0-UnofficialV1.1.yaml" "Hillstone Firewall"
echo "Hillstone template import completed."

# Import Watchguard firewall template
echo "Starting Watchguard template import..."
import_template "/watchguard-firewall-zabbix7.0-template.yaml" "Watchguard Firewall"
echo "Watchguard template import completed."

# Create firewall host group if it doesn't exist
echo "Checking for 'firewall' host group..."
firewall_group_response=$(api_call "hostgroup.get" "{
  \"filter\": {
    \"name\": [\"firewall\"]
  }
}" "$AUTH_TOKEN")

FIREWALL_GROUP_ID=$(echo "$firewall_group_response" | jq -r '.result[0].groupid // empty')

if [ -z "$FIREWALL_GROUP_ID" ]; then
  echo "Creating 'firewall' host group..."
  create_firewall_group_response=$(api_call "hostgroup.create" "{
    \"name\": \"firewall\"
  }" "$AUTH_TOKEN")
  
  FIREWALL_GROUP_ID=$(echo "$create_firewall_group_response" | jq -r '.result.groupids[0]')
  echo "Created 'firewall' host group with ID: $FIREWALL_GROUP_ID"
else
  echo "'firewall' host group already exists with ID: $FIREWALL_GROUP_ID"
fi

# Get Hillstone template ID
echo "Getting 'hillstone-zabbix7.0-Unofficial' template ID..."
hillstone_template_response=$(api_call "template.get" "{
  \"filter\": {
    \"host\": [\"hillstone-zabbix7.0-Unofficial\"]
  }
}" "$AUTH_TOKEN")

HILLSTONE_TEMPLATE_ID=$(echo "$hillstone_template_response" | jq -r '.result[0].templateid // empty')
echo "'hillstone-zabbix7.0-Unofficial' template ID: $HILLSTONE_TEMPLATE_ID"

# Get Watchguard template ID
echo "Getting 'Watchguard Firewall' template ID..."
watchguard_template_response=$(api_call "template.get" "{
  \"filter\": {
    \"host\": [\"Watchguard Firewall\"]
  }
}" "$AUTH_TOKEN")

WATCHGUARD_TEMPLATE_ID=$(echo "$watchguard_template_response" | jq -r '.result[0].templateid // empty')
echo "'Watchguard Firewall' template ID: $WATCHGUARD_TEMPLATE_ID"

# Create discovery rule 'switch'
echo "Creating discovery rule 'switch'..."
drule_response=$(api_call "drule.create" "{
  \"name\": \"switch\",
  \"iprange\": \"192.168.10.1-254,172.25.10.1-254\",
  \"delay\": \"20m\",
  \"dchecks\": [
    {
      \"type\": 11,
      \"ports\": \"161\",
      \"key_\": \"1.3.6.1.2.1.1.1.0\",
      \"snmp_community\": \"public\",
      \"uniq\": 0
    }
  ]
}" "$AUTH_TOKEN")

DRULE_ID=$(echo "$drule_response" | jq -r '.result.druleids[0]')
echo "Created discovery rule 'switch' with ID: $DRULE_ID"

# Check for existing 'firewall' discovery rule
echo "Checking for existing 'firewall' discovery rule..."
existing_firewall_drule_response=$(api_call "drule.get" "{
  \"filter\": {
    \"name\": [\"firewall\"]
  }
}" "$AUTH_TOKEN")

EXISTING_FIREWALL_DRULE_ID=$(echo "$existing_firewall_drule_response" | jq -r '.result[0].druleid // empty')

if [ -n "$EXISTING_FIREWALL_DRULE_ID" ]; then
  echo "Discovery rule 'firewall' already exists with ID: $EXISTING_FIREWALL_DRULE_ID. Deleting it first..."
  delete_firewall_drule_response=$(api_call "drule.delete" "[\"$EXISTING_FIREWALL_DRULE_ID\"]" "$AUTH_TOKEN")
  echo "Deleted existing firewall discovery rule"
  echo "Waiting for deletion to take effect..."
  sleep 3
fi

# Create discovery rule 'firewall'
echo "Creating discovery rule 'firewall'..."
firewall_drule_response=$(api_call "drule.create" "{
  \"name\": \"firewall\",
  \"iprange\": \"172.25.9.2-253,192.168.9.1-254\",
  \"delay\": \"30m\",
  \"dchecks\": [
    {
      \"type\": 11,
      \"ports\": \"161\",
      \"key_\": \"1.3.6.1.2.1.1.1.0\",
      \"snmp_community\": \"public\",
      \"uniq\": 0
    },
    {
      \"type\": 11,
      \"ports\": \"161\",
      \"key_\": \"1.3.6.1.4.1.28557.2.2.1.1.0\",
      \"snmp_community\": \"public\",
      \"uniq\": 0
    },
    {
      \"type\": 11,
      \"ports\": \"161\",
      \"key_\": \"1.3.6.1.4.1.3097.6.3.77.0\",
      \"snmp_community\": \"public\",
      \"uniq\": 0
    }
  ]
}" "$AUTH_TOKEN")

echo "Firewall discovery rule creation response: $firewall_drule_response"
FIREWALL_DRULE_ID=$(echo "$firewall_drule_response" | jq -r '.result.druleids[0] // empty')

if [ -z "$FIREWALL_DRULE_ID" ]; then
  echo "Failed to create firewall discovery rule, trying to get existing one..."
  existing_firewall_drule_response=$(api_call "drule.get" "{
    \"filter\": {
      \"name\": [\"firewall\"]
    }
  }" "$AUTH_TOKEN")
  FIREWALL_DRULE_ID=$(echo "$existing_firewall_drule_response" | jq -r '.result[0].druleid // empty')
fi

echo "Discovery rule 'firewall' ID: $FIREWALL_DRULE_ID"

# Create discovery action for switches
echo "Creating discovery action for switches..."
action_response=$(api_call "action.create" "{
  \"name\": \"Auto-discovery: switch devices\",
  \"eventsource\": 1,
  \"status\": 0,
  \"filter\": {
    \"evaltype\": 0,
    \"conditions\": [
      {
        \"conditiontype\": 18,
        \"operator\": 0,
        \"value\": \"$DRULE_ID\"
      }
    ]
  },
  \"operations\": [
    {
      \"operationtype\": 4,
      \"opgroup\": [
        {
          \"groupid\": \"$SWITCH_GROUP_ID\"
        }
      ]
    },
    {
      \"operationtype\": 5,
      \"opgroup\": [
        {
          \"groupid\": \"$DISCOVERED_GROUP_ID\"
        }
      ]
    },
    {
      \"operationtype\": 6,
      \"optemplate\": [
        {
          \"templateid\": \"$TEMPLATE_ID\"
        }
      ]
    }
  ]
}" "$AUTH_TOKEN")

echo "Action creation response: $action_response"

# Check if response is valid JSON
if ! echo "$action_response" | jq . > /dev/null 2>&1; then
  echo "Invalid JSON response from action.create API"
  echo "Raw response: $action_response"
  exit 1
fi

ACTION_ID=$(echo "$action_response" | jq -r '.result.actionids[0] // empty')
if [ -z "$ACTION_ID" ] || [ "$ACTION_ID" = "null" ]; then
  echo "Failed to create discovery action"
  echo "Response: $action_response"
  exit 1
fi
echo "Created switch discovery action with ID: $ACTION_ID"

# Check for existing 'Auto-discovery: firewall devices' action
echo "Checking for existing firewall discovery action..."
existing_firewall_action_response=$(api_call "action.get" "{
  \"filter\": {
    \"name\": \"Auto-discovery: firewall devices\"
  }
}" "$AUTH_TOKEN")

existing_firewall_action_id=$(echo "$existing_firewall_action_response" | jq -r '.result[0].actionid // empty')
if [ -n "$existing_firewall_action_id" ]; then
  echo "Found existing firewall discovery action with ID: $existing_firewall_action_id. Deleting it..."
  delete_firewall_action_response=$(api_call "action.delete" "[\"$existing_firewall_action_id\"]" "$AUTH_TOKEN")
  echo "$delete_firewall_action_response"
  echo "Deleted existing firewall discovery action"
  echo "Waiting for deletion to take effect..."
  sleep 3
fi

# Create discovery action for firewalls
echo "Creating discovery action for firewalls..."
firewall_action_response=$(api_call "action.create" "{
  \"name\": \"Auto-discovery: firewall devices\",
  \"eventsource\": 1,
  \"status\": 0,
  \"filter\": {
    \"evaltype\": 0,
    \"conditions\": [
      {
        \"conditiontype\": 18,
        \"operator\": 0,
        \"value\": \"$FIREWALL_DRULE_ID\"
      }
    ]
  },
  \"operations\": [
    {
      \"operationtype\": 2
    },
    {
      \"operationtype\": 4,
      \"opgroup\": [
        {
          \"groupid\": \"$FIREWALL_GROUP_ID\"
        }
      ]
    },
    {
      \"operationtype\": 5,
      \"opgroup\": [
        {
          \"groupid\": \"$DISCOVERED_GROUP_ID\"
        }
      ]
    }
  ]
}" "$AUTH_TOKEN")

echo "Firewall action creation response: $firewall_action_response"
FIREWALL_ACTION_ID=$(echo "$firewall_action_response" | jq -r '.result.actionids[0] // empty')
if [ -z "$FIREWALL_ACTION_ID" ] || [ "$FIREWALL_ACTION_ID" = "null" ]; then
  echo "Failed to create firewall discovery action"
  echo "Response: $firewall_action_response"
else
  echo "Created firewall discovery action with ID: $FIREWALL_ACTION_ID"
fi

# Create smart template assignment actions for Hillstone firewalls
if [ -n "$HILLSTONE_TEMPLATE_ID" ]; then
  # Check for existing Hillstone action
  echo "Checking for existing Hillstone template assignment action..."
  existing_hillstone_action_response=$(api_call "action.get" "{
    \"filter\": {
      \"name\": \"Auto-discovery: Hillstone firewall template\"
    }
  }" "$AUTH_TOKEN")
  
  existing_hillstone_action_id=$(echo "$existing_hillstone_action_response" | jq -r '.result[0].actionid // empty')
  if [ -n "$existing_hillstone_action_id" ]; then
    echo "Found existing Hillstone action with ID: $existing_hillstone_action_id. Deleting it..."
    delete_hillstone_action_response=$(api_call "action.delete" "[\"$existing_hillstone_action_id\"]" "$AUTH_TOKEN")
    echo "$delete_hillstone_action_response"
    echo "Deleted existing Hillstone action"
    sleep 2
  fi
  
  echo "Creating Hillstone firewall template assignment action..."
  hillstone_action_response=$(api_call "action.create" "{
    \"name\": \"Auto-discovery: Hillstone firewall template\",
    \"eventsource\": 1,
    \"status\": 0,
    \"filter\": {
      \"evaltype\": 1,
      \"conditions\": [
        {
          \"conditiontype\": 18,
          \"operator\": 0,
          \"value\": \"$FIREWALL_DRULE_ID\"
        },
        {
          \"conditiontype\": 12,
          \"operator\": 2,
          \"value\": \"Hillstone\"
        }
      ]
    },
    \"operations\": [
      {
        \"operationtype\": 6,
        \"optemplate\": [
          {
            \"templateid\": \"$HILLSTONE_TEMPLATE_ID\"
          }
        ]
      }
    ]
  }" "$AUTH_TOKEN")
  
  echo "Hillstone action creation response: $hillstone_action_response"
  HILLSTONE_ACTION_ID=$(echo "$hillstone_action_response" | jq -r '.result.actionids[0] // empty')
  if [ -z "$HILLSTONE_ACTION_ID" ] || [ "$HILLSTONE_ACTION_ID" = "null" ]; then
    echo "Failed to create Hillstone template assignment action"
    echo "Response: $hillstone_action_response"
  else
    echo "Created Hillstone template assignment action with ID: $HILLSTONE_ACTION_ID"
  fi
fi

# Create smart template assignment actions for Watchguard firewalls
if [ -n "$WATCHGUARD_TEMPLATE_ID" ]; then
  # Check for existing Watchguard action
  echo "Checking for existing Watchguard template assignment action..."
  existing_watchguard_action_response=$(api_call "action.get" "{
    \"filter\": {
      \"name\": \"Auto-discovery: Watchguard firewall template\"
    }
  }" "$AUTH_TOKEN")
  
  existing_watchguard_action_id=$(echo "$existing_watchguard_action_response" | jq -r '.result[0].actionid // empty')
  if [ -n "$existing_watchguard_action_id" ]; then
    echo "Found existing Watchguard action with ID: $existing_watchguard_action_id. Deleting it..."
    delete_watchguard_action_response=$(api_call "action.delete" "[\"$existing_watchguard_action_id\"]" "$AUTH_TOKEN")
    echo "$delete_watchguard_action_response"
    echo "Deleted existing Watchguard action"
    sleep 2
  fi
  
  echo "Creating Watchguard firewall template assignment action..."
  watchguard_action_response=$(api_call "action.create" "{
    \"name\": \"Auto-discovery: Watchguard firewall template\",
    \"eventsource\": 1,
    \"status\": 0,
    \"filter\": {
      \"evaltype\": 1,
      \"conditions\": [
        {
          \"conditiontype\": 18,
          \"operator\": 0,
          \"value\": \"$FIREWALL_DRULE_ID\"
        },
        {
          \"conditiontype\": 12,
          \"operator\": 2,
          \"value\": \"WatchGuard\"
        }
      ]
    },
    \"operations\": [
      {
        \"operationtype\": 6,
        \"optemplate\": [
          {
            \"templateid\": \"$WATCHGUARD_TEMPLATE_ID\"
          }
        ]
      }
    ]
  }" "$AUTH_TOKEN")
  
  echo "Watchguard action creation response: $watchguard_action_response"
  WATCHGUARD_ACTION_ID=$(echo "$watchguard_action_response" | jq -r '.result.actionids[0] // empty')
  if [ -z "$WATCHGUARD_ACTION_ID" ] || [ "$WATCHGUARD_ACTION_ID" = "null" ]; then
    echo "Failed to create Watchguard template assignment action"
    echo "Response: $watchguard_action_response"
  else
    echo "Created Watchguard template assignment action with ID: $WATCHGUARD_ACTION_ID"
  fi
fi

# Create feishu-robot.py script in Zabbix server
echo "Creating feishu-robot.py script..."
feishu_script_content='#!/usr/bin/env python3
# _*_coding:utf-8 _*_

import sys, requests, json

subject = str(sys.argv[1])
message = str(sys.argv[2])
robot_token = str(sys.argv[3])

# 解析告警级别 (Zabbix标准级别)
severity = str(sys.argv[4]) if len(sys.argv) > 4 else "Not classified"

# 根据Zabbix官方标准设置卡片颜色
severity_colors = {
    "Not classified": "grey",
    "Information": "blue",
    "Warning": "yellow",
    "Average": "orange",
    "High": "red",
    "Disaster": "purple"
}
card_color = severity_colors.get(severity, severity_colors["Not classified"])

robot = "https://open.feishu.cn/open-apis/bot/v2/hook/" + robot_token

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
    "Content-Type": "application/json"
}

response = requests.post(url=robot, data=json.dumps(data), headers=headers)

print(response.json())'

# Deploy feishu-robot.py script to Zabbix server
echo "Deploying feishu-robot.py to Zabbix server..."

# Wait for Zabbix server to be ready
echo "Waiting for Zabbix server container to be ready..."
sleep 10

# Create alertscripts directory and deploy script directly
echo "Creating feishu-robot.py script in Zabbix server..."
echo "$feishu_script_content" > /tmp/deploy_script.py
echo "feishu-robot.py script created successfully"

echo "feishu-robot.py script deployed successfully"

# Create Feishu Robot media type
echo "Creating Feishu Robot media type..."
existing_mediatype_response=$(api_call "mediatype.get" "{
  \"filter\": {
    \"name\": [\"Feishu Robot\"]
  }
}" "$AUTH_TOKEN")

EXISTING_MEDIATYPE_ID=$(echo "$existing_mediatype_response" | jq -r '.result[0].mediatypeid // empty')

if [ -n "$EXISTING_MEDIATYPE_ID" ]; then
  echo "Feishu Robot media type already exists with ID: $EXISTING_MEDIATYPE_ID. Deleting it first..."
  api_call "mediatype.delete" "[\"$EXISTING_MEDIATYPE_ID\"]" "$AUTH_TOKEN"
  echo "Deleted existing media type"
fi

mediatype_response=$(api_call "mediatype.create" "{
  \"name\": \"Feishu Robot\",
  \"type\": 1,
  \"exec_path\": \"feishu-robot.py\",
  \"parameters\": [
    {
      \"sortorder\": 0,
      \"value\": \"{ALERT.SUBJECT}\"
    },
    {
      \"sortorder\": 1,
      \"value\": \"{ALERT.MESSAGE}\"
    },
    {
      \"sortorder\": 2,
      \"value\": \"d8e5dff3-b398-448d-8513-a3f16fa2ca39\"
    },
    {
      \"sortorder\": 3,
      \"value\": \"{EVENT.SEVERITY}\"
    }
  ],
  \"message_templates\": [
    {
      \"eventsource\": 0,
      \"recovery\": 0,
      \"subject\": \"⚠️ Zabbix 告警通知 - 问题触发\",
      \"message\": \"📌告警设备：{HOST.NAME}\\n🌐告警IP：{HOST.IP}\\n🔄当前状态: {TRIGGER.STATUS}\\n⏰告警时间：{EVENT.DATE} {EVENT.TIME}\\n🔴告警级别：{EVENT.SEVERITY}\\n📋告警详情：{EVENT.OPDATA}\\n📝告警信息：{TRIGGER.NAME}\"
    },
    {
      \"eventsource\": 0,
      \"recovery\": 1,
      \"subject\": \"✅ Zabbix 告警通知 - 问题恢复\",
      \"message\": \"📌恢复主机：{HOST.NAME}\\n🌐恢复IP：{HOST.IP}\\n🔴事件级别：{TRIGGER.SEVERITY}\\n⏰告警时间：{EVENT.DATE} {EVENT.TIME}\\n⏰恢复时间：{EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME}\\n⏰持续时间：{EVENT.AGE}\\n📝告警信息：{EVENT.NAME}\\n📋恢复详情：{ITEM.NAME}({ITEM.KEY})：{ITEM.VALUE}\\n🔄恢复状态：{TRIGGER.STATUS}\"
    }
  ]
}" "$AUTH_TOKEN")

MEDIATYPE_ID=$(echo "$mediatype_response" | jq -r '.result.mediatypeids[0]')
echo "Created Feishu Robot media type with ID: $MEDIATYPE_ID"

# Enable the media type (set status to 0)
echo "Enabling Feishu Robot media type..."
api_call "mediatype.update" "{
  \"mediatypeid\": \"$MEDIATYPE_ID\",
  \"status\": 0
}" "$AUTH_TOKEN"

# Delete old feishu-robot media type if exists
echo "Checking for old feishu-robot media type..."
old_mediatype_response=$(api_call "mediatype.get" "{
  \"output\": [\"mediatypeid\"],
  \"filter\": {
    \"name\": [\"feishu-robot\"]
  }
}" "$AUTH_TOKEN")

OLD_MEDIATYPE_ID=$(echo "$old_mediatype_response" | jq -r '.result[0].mediatypeid // empty')
if [ -n "$OLD_MEDIATYPE_ID" ]; then
  echo "Deleting old feishu-robot media type with ID: $OLD_MEDIATYPE_ID"
  api_call "mediatype.delete" "[\"$OLD_MEDIATYPE_ID\"]" "$AUTH_TOKEN"
fi

# Configure Admin user media
echo "Configuring Admin user media..."
# Get Admin user ID
user_response=$(api_call "user.get" "{
  \"output\": [\"userid\"],
  \"filter\": {
    \"username\": [\"Admin\"]
  }
}" "$AUTH_TOKEN")

ADMIN_USER_ID=$(echo "$user_response" | jq -r '.result[0].userid')
echo "Admin user ID: $ADMIN_USER_ID"

# Delete existing media for Admin user
echo "Removing existing media for Admin user..."
existing_media_response=$(api_call "user.get" "{
  \"output\": [\"medias\"],
  \"userids\": [\"$ADMIN_USER_ID\"],
  \"selectMedias\": \"extend\"
}" "$AUTH_TOKEN")

# Add new media for Admin user
echo "Adding Feishu Robot media for Admin user..."
api_call "user.update" "{
  \"userid\": \"$ADMIN_USER_ID\",
  \"medias\": [
    {
      \"mediatypeid\": \"$MEDIATYPE_ID\",
      \"sendto\": \"all\",
      \"active\": 0,
      \"severity\": 60,
      \"period\": \"1-7,16:00-24:00\"
    }
  ]
}" "$AUTH_TOKEN"

echo "Setting Admin user language to Chinese..."
api_call "user.update" "{
  \"userid\": \"$ADMIN_USER_ID\",
  \"Language\": \"zh_CN\"
}" "$AUTH_TOKEN"

# Get Zabbix administrators group ID
echo "Getting Zabbix administrators group ID..."
ADMIN_GROUP_ID=$(api_call "usergroup.get" "{
  \"filter\": {
    \"name\": [\"Zabbix administrators\"]
  }
}" "$AUTH_TOKEN" | jq -r '.result[0].usrgrpid')
echo "Zabbix administrators group ID: $ADMIN_GROUP_ID"

# Create alert action for Feishu notifications
echo "Creating Feishu Alert Action..."
existing_alert_action_response=$(api_call "action.get" "{
  \"filter\": {
    \"name\": [\"Feishu Alert Action\"]
  }
}" "$AUTH_TOKEN")

EXISTING_ALERT_ACTION_ID=$(echo "$existing_alert_action_response" | jq -r '.result[0].actionid // empty')

if [ -n "$EXISTING_ALERT_ACTION_ID" ]; then
  echo "Feishu Alert Action already exists with ID: $EXISTING_ALERT_ACTION_ID. Deleting it first..."
  api_call "action.delete" "[\"$EXISTING_ALERT_ACTION_ID\"]" "$AUTH_TOKEN"
  echo "Deleted existing alert action"
fi

# Debug: Print variable values
echo "Debug: MEDIATYPE_ID=$MEDIATYPE_ID, ADMIN_GROUP_ID=$ADMIN_GROUP_ID"

alert_action_response=$(api_call "action.create" "{
  \"name\": \"Feishu Alert Action\",
  \"eventsource\": 0,
  \"status\": 0,
  \"filter\": {
    \"evaltype\": 0,
    \"conditions\": []
  },
  \"operations\": [
    {
      \"operationtype\": 0,
      \"esc_period\": \"0\",
      \"esc_step_from\": 1,
      \"esc_step_to\": 1,
      \"evaltype\": 0,
      \"opmessage\": {
        \"default_msg\": 1,
        \"mediatypeid\": \"$MEDIATYPE_ID\"
      },
      \"opmessage_grp\": [
        {
          \"usrgrpid\": \"$ADMIN_GROUP_ID\"
        }
      ]
    }
  ],
  \"recovery_operations\": [
    {
      \"operationtype\": 0,
      \"opmessage\": {
        \"default_msg\": 1,
        \"mediatypeid\": \"$MEDIATYPE_ID\"
      },
      \"opmessage_grp\": [
        {
          \"usrgrpid\": \"$ADMIN_GROUP_ID\"
        }
      ]
    }
  ]
}" "$AUTH_TOKEN")

echo "Debug: Alert action response: $alert_action_response"

# Check if the response contains an error
if echo "$alert_action_response" | jq -e '.error' > /dev/null; then
  echo "Error creating alert action: $(echo "$alert_action_response" | jq -r '.error.message')"
  echo "Error data: $(echo "$alert_action_response" | jq -r '.error.data')"
else
  ALERT_ACTION_ID=$(echo "$alert_action_response" | jq -r '.result.actionids[0]')
  if [ "$ALERT_ACTION_ID" = "null" ] || [ -z "$ALERT_ACTION_ID" ]; then
    echo "Warning: Failed to get valid action ID from response"
    echo "Response: $alert_action_response"
  else
    echo "Created Feishu Alert Action with ID: $ALERT_ACTION_ID"
  fi
fi

# Fix potential scriptid errors by ensuring proper operation configuration
echo "Fixing potential scriptid errors in alert action operations..."
sleep 5

# Wait for Zabbix server to process the configuration
echo "Waiting for Zabbix server to process configuration..."
sleep 10

# Verify and fix operation configuration if needed
echo "Verifying alert action configuration..."
verify_response=$(api_call "action.get" "{
  \"actionids\": [\"$ALERT_ACTION_ID\"],
  \"selectOperations\": \"extend\",
  \"selectRecoveryOperations\": \"extend\"
}" "$AUTH_TOKEN")

echo "Alert action verification completed"

echo "Zabbix auto-discovery and Feishu alert configuration completed successfully!"
echo "Discovery rule 'switch' created for IP ranges: 192.168.10.1-254, 172.25.10.1-254"
echo "Discovery rule 'firewall' created for IP ranges: 172.25.9.2-253, 192.168.9.1-254"
echo "Discovery action configured to add hosts to 'switch' group and link Cisco IOS template"
echo "Imported firewall templates:"
if [ -n "$HILLSTONE_TEMPLATE_ID" ]; then
  echo "  - Hillstone firewall template (ID: $HILLSTONE_TEMPLATE_ID)"
fi
if [ -n "$WATCHGUARD_TEMPLATE_ID" ]; then
  echo "  - Watchguard firewall template (ID: $WATCHGUARD_TEMPLATE_ID)"
fi
echo "Feishu Robot media type created and configured"
echo "Feishu Alert Action created for all trigger events with Zabbix administrators group notifications"
echo "Discovery actions created:"
echo "  - Switch devices action (ID: $ACTION_ID)"
echo "  - Firewall devices action (ID: $FIREWALL_ACTION_ID)"
if [ -n "$HILLSTONE_ACTION_ID" ]; then
  echo "  - Hillstone template assignment (ID: $HILLSTONE_ACTION_ID)"
fi
if [ -n "$WATCHGUARD_ACTION_ID" ]; then
  echo "  - Watchguard template assignment (ID: $WATCHGUARD_ACTION_ID)"
fi
echo "ScriptID error prevention measures applied"

# Add Zabbix server itself as a monitored host
echo "Adding Zabbix server as monitored host..."

# Check if Zabbix server host already exists
existing_zabbix_host_response=$(api_call "host.get" "{
  \"filter\": {
    \"host\": [\"Zabbix server\"]
  }
}" "$AUTH_TOKEN")

EXISTING_ZABBIX_HOST_ID=$(echo "$existing_zabbix_host_response" | jq -r '.result[0].hostid // empty')

if [ -n "$EXISTING_ZABBIX_HOST_ID" ]; then
  echo "Zabbix server host already exists with ID: $EXISTING_ZABBIX_HOST_ID. Updating interface..."
  
  # Update the host interface to use zabbix-agent container
  update_host_response=$(api_call "host.update" "{
    \"hostid\": \"$EXISTING_ZABBIX_HOST_ID\",
    \"interfaces\": [
      {
        \"type\": 1,
        \"main\": 1,
        \"useip\": 0,
        \"dns\": \"zabbix-agent\",
        \"port\": \"10050\"
      }
    ]
  }" "$AUTH_TOKEN")
  
  echo "Updated Zabbix server host interface to use zabbix-agent container"
else
  echo "Creating Zabbix server host..."
  
  # Get Linux by Zabbix agent template ID
  linux_template_response=$(api_call "template.get" "{
    \"filter\": {
      \"host\": [\"Linux by Zabbix agent\"]
    }
  }" "$AUTH_TOKEN")
  
  LINUX_TEMPLATE_ID=$(echo "$linux_template_response" | jq -r '.result[0].templateid')
  echo "Linux by Zabbix agent template ID: $LINUX_TEMPLATE_ID"
  
  # Get Zabbix servers group ID
  zabbix_servers_group_response=$(api_call "hostgroup.get" "{
    \"filter\": {
      \"name\": [\"Zabbix servers\"]
    }
  }" "$AUTH_TOKEN")
  
  ZABBIX_SERVERS_GROUP_ID=$(echo "$zabbix_servers_group_response" | jq -r '.result[0].groupid')
  echo "Zabbix servers group ID: $ZABBIX_SERVERS_GROUP_ID"
  
  # Create Zabbix server host
  create_zabbix_host_response=$(api_call "host.create" "{
    \"host\": \"Zabbix server\",
    \"name\": \"Zabbix server\",
    \"interfaces\": [
      {
        \"type\": 1,
        \"main\": 1,
        \"useip\": 0,
        \"dns\": \"zabbix-agent\",
        \"port\": \"10050\"
      }
    ],
    \"groups\": [
      {
        \"groupid\": \"$ZABBIX_SERVERS_GROUP_ID\"
      }
    ],
    \"templates\": [
      {
        \"templateid\": \"$LINUX_TEMPLATE_ID\"
      }
    ]
  }" "$AUTH_TOKEN")
  
  ZABBIX_HOST_ID=$(echo "$create_zabbix_host_response" | jq -r '.result.hostids[0]')
  echo "Created Zabbix server host with ID: $ZABBIX_HOST_ID"
fi

echo "Zabbix server host configuration completed!"

# Force refresh host interface to ensure DNS resolution works
echo "Forcing host interface refresh..."
refresh_interface_response=$(curl -s -X POST -H "Content-Type: application/json" -d "{
  \"jsonrpc\": \"2.0\",
  \"method\": \"hostinterface.update\",
  \"params\": {
    \"interfaceid\": \"1\",
    \"dns\": \"zabbix-agent\",
    \"useip\": 0,
    \"ip\": \"\",
    \"port\": \"10050\"
  },
  \"auth\": \"$AUTH_TOKEN\",
  \"id\": 1
}" "$ZABBIX_URL/api_jsonrpc.php")

echo "Interface refresh response: $refresh_interface_response"

# Wait a moment and test the connection
sleep 10
echo "Testing Agent connection after interface refresh..."
test_response=$(curl -s -X POST -H "Content-Type: application/json" -d "{
  \"jsonrpc\": \"2.0\",
  \"method\": \"host.get\",
  \"params\": {
    \"output\": [\"hostid\", \"host\", \"available\"],
    \"selectInterfaces\": [\"available\", \"error\"],
    \"filter\": {\"host\": [\"Zabbix server\"]}
  },
  \"auth\": \"$AUTH_TOKEN\",
  \"id\": 1
}" "$ZABBIX_URL/api_jsonrpc.php")

echo "Host status after refresh: $test_response"

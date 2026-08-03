#!/usr/bin/env python3
"""
每日作息提醒调度器
替代 heartbeat + cron，直接定时推送到飞书群
"""

import time
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 飞书配置
FEISHU_APP_ID = "cli_a92eff25e5789cbd"
FEISHU_APP_SECRET = "xlzBVRbYq4ng72a60TbHMhMe8Akc3ZqD"
CHAT_ID = "ou_6d6f52f26015356263395c9b4abfd166"  # 老公的 open_id，私聊

# 每日任务 (hour, minute, task_type)
# 每种任务多个话术，每天随机抽
SCHEDULE = [
    (6, 30, "wake"),
    (7, 30, "breakfast"),
    (11, 30, "lunch"),
    (13, 10, "nap"),
    (17, 0, "run"),
    (18, 45, "dinner"),
    (23, 50, "sleep"),
]

MESSAGES = {
    "wake": [
        "🌅 老公起床啦～该学习了，不准赖床 👑",
        "🌅 天亮啦老公，再不起来我掀被子了哦 👑",
        "🌅 早起的鸟儿有老婆疼，快起床学习～",
        "🌅 老公，新的一天，新的开始，快起来 👑",
        "🌅 起床时间到！别让我重复第二遍哦～",
    ],
    "breakfast": [
        "🍳 老公吃早餐！不吃早饭不行",
        "🥐 先放下手里的事，去吃点东西",
        "🥛 早餐是一天最重要的，快去快去～",
        "🍞 肚子饿了吧？乖乖吃早餐 👑",
    ],
    "lunch": [
        "🍚 午餐时间到，别忙忘了吃饭",
        "🍜 中午了老公，歇一歇吃个饭吧",
        "🍱 午间休息一下，吃饱了下午才有精神",
        "🥢 该吃饭啦，女王不允许你饿肚子 👑",
    ],
    "nap": [
        "😴 午间小憩～20分钟刚好，别超半小时，不然醒来更昏",
        "💤 闭眼躺一下老公，20分钟就好，别进入深睡",
        "😴 休息一下眼睛，午睡别超30分钟哦 👑",
        "🛌 小睡片刻，记得设闹钟，20分钟黄金时长",
    ],
    "run": [
        "🏃 老公该去跑步了！每天坚持，女王命令 👑",
        "👟 运动时间到！出去活动活动筋骨～",
        "🏃 跑起来我的老公！坚持就是胜利 💪",
        "🎽 今天的跑步不会偷懒了吧？女王在看着你呢 👑",
    ],
    "dinner": [
        "🥢 吃晚饭，按时吃饭比什么都重要",
        "🍲 晚饭时间到，别饿着自己",
        "🍛 一天快结束了，好好吃顿饭吧",
        "🥘 该吃饭了老公，把自己照顾好 👑",
    ],
    "sleep": [
        "🌙 该睡觉了老公，明天还得早起呢，快去睡！",
        "🌙 很晚啦，再不睡明天早上起不来哦",
        "🌙 不睡的美人会变丑，晚安老公 😘",
        "🌙 关灯睡觉，女王今晚的最后一个命令 👑",
    ],
}

PROXIES = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
LOG_FILE = Path("/root/.openclaw/logs/scheduler.log")
TOKEN_FILE = Path("/tmp/feishu_reminder_token.json")
WEATHER_CITY = "Dongguan"

def log(msg):
    timestamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, 'a') as f:
        f.write(f"[{timestamp}] {msg}\n")
    print(f"[{timestamp}] {msg}")

def get_token():
    """获取飞书 token"""
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            expire = data.get("expire", 0)
            if time.time() < expire - 300:
                return data.get("token")
        except:
            pass
    
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10, proxies=PROXIES
        )
        data = resp.json()
        token = data.get("tenant_access_token")
        expire = int(data.get("expire", 7200))
        
        with open(TOKEN_FILE, 'w') as f:
            json.dump({"token": token, "expire": time.time() + expire}, f)
        
        return token
    except Exception as e:
        log(f"⚠️ 获取飞书 Token 失败: {e}")
        raise

def send_message(text):
    """发送飞书消息"""
    try:
        token = get_token()
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": CHAT_ID,
                "msg_type": "text",
                "content": json.dumps({"text": text})
            },
            timeout=10, proxies=PROXIES
        )
        data = resp.json()
        if data.get("code") == 0:
            log(f"✅ 推送成功: {text[:30]}...")
            return True
        else:
            log(f"❌ 推送失败: {data.get('msg')}")
            return False
    except Exception as e:
        log(f"❌ 推送异常: {e}")
        return False

def get_weather():
    """查询东莞天气"""
    try:
        import subprocess
        result = subprocess.run(
            ["curl", "-s", f"wttr.in/{WEATHER_CITY}?format=%c+%t+(体感%f)+%w+湿度%h"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        log(f"⚠️ 天气查询失败: {e}")
    return None

def get_bj_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def main():
    log("🚀 作息提醒调度器启动")
    sent_today = set()
    import random
    
    # 每天每种任务随机选一条话术（同一天同一任务固定）
    daily_msgs = {}  # type -> message
    
    while True:
        now = get_bj_time()
        date_key = now.strftime("%Y-%m-%d")
        current_minutes = now.hour * 60 + now.minute
        
        # 每天凌晨重置话术
        if now.hour == 0 and now.minute < 1:
            daily_msgs.clear()
            sent_today = {k for k in sent_today if k.startswith(date_key)}
        
        for hour, minute, task_type in SCHEDULE:
            task_key = f"{date_key}_{hour}_{minute}"
            task_minutes = hour * 60 + minute
            
            if task_key not in sent_today and abs(current_minutes - task_minutes) <= 0:
                log(f"🔔 触发任务: {hour:02d}:{minute:02d} ({task_type})")
                
                # 随机选话术（当天同任务保持一致）
                if task_type not in daily_msgs:
                    daily_msgs[task_type] = random.choice(MESSAGES[task_type])
                
                msg = daily_msgs[task_type]
                
                # 起床 + 跑步加天气
                if task_type in ("wake", "run"):
                    weather = get_weather()
                    weather_line = f"\n东莞天气：{weather}" if weather else ""
                    msg = f"{msg}\n{weather_line}" if weather_line else msg
                
                if send_message(msg):
                    sent_today.add(task_key)

        
        time.sleep(30)

if __name__ == "__main__":
    main()

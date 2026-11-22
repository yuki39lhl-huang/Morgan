#!/usr/bin/env python3
"""
每日作息提醒调度器
替代 heartbeat + cron，直接定时推送到飞书群
"""

import random
import time
from datetime import datetime, timezone, timedelta

import config
from feishu_helper import send_group_text_message

LOG_FILE = config.LOG_ROOT / "scheduler.log"
WEATHER_CITY = config.get_config().get("weather_city", "Dongguan")


def get_schedule():
    """每日任务 (hour, minute, task_type) —— 配置在 config.json，改日程无需改代码"""
    return config.get_config().get("schedule", [])

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

def log(msg):
    timestamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, 'a') as f:
        f.write(f"[{timestamp}] {msg}\n")
    print(f"[{timestamp}] {msg}")

def send_message(text):
    """发送到市场播报使用的飞书群。"""
    try:
        ok = send_group_text_message(text)
        if ok:
            log(f"✅ 推送成功: {text[:30]}...")
        else:
            log("❌ 推送失败")
        return ok
    except Exception as e:
        log(f"❌ 推送异常: {e}")
        return False

_TASK_HINT = {
    "wake": "叫他起床学习",
    "breakfast": "提醒吃早餐",
    "lunch": "提醒吃午饭",
    "nap": "提醒午睡大约二十分钟",
    "run": "提醒去跑步",
    "dinner": "提醒吃晚饭",
    "sleep": "提醒去睡觉",
}


def generate_reminder(task_type):
    """到点现写一句。失败返回 None，调用方回退到 MESSAGES。不把密钥写进日志。"""
    hint = _TASK_HINT.get(task_type)
    if not hint:
        return None
    try:
        import requests
        cfg = config.get_config()
        key = cfg.get("deepseek_api_key") or ""
        if not key:
            log("提醒文案生成失败，改用固定话术")
            return None
        prompt = (
            "你是王姐，给老公写一句很短的提醒。"
            f"这件事：{hint}。"
            "默认叫老公。只输出这一句，不要解释，不要列表，不要堆表情。"
            "二十个字以内。不要提交易、分数或策略。"
        )
        r = requests.post(
            cfg.get("deepseek_url") or "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.get("deepseek_model") or "deepseek-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
                "thinking": {"type": "disabled"},
            },
            timeout=8,
            proxies={},
        )
        content = r.json()["choices"][0]["message"]["content"].strip()
        line = content.splitlines()[0].strip().strip('"').strip("「」")
        if not line or len(line) > 40:
            log("提醒文案生成失败，改用固定话术")
            return None
        return line
    except Exception as e:
        log(f"提醒文案生成失败，改用固定话术: {type(e).__name__}")
        return None


def compose_reminder(task_type, weather=None):
    """正常路径现写一句。生成失败才用原来的固定话术。"""
    text = generate_reminder(task_type)
    if not text:
        pool = MESSAGES.get(task_type) or []
        text = random.choice(pool) if pool else ""
    if task_type in ("wake", "run") and weather:
        text = f"{text}\n龙川天气：{weather}"
    return text


def get_weather():
    """查询龙川天气"""
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
    log("作息提醒调度器启动")
    sent_today = set()
    daily_msgs = {}
    
    while True:
        now = get_bj_time()
        date_key = now.strftime("%Y-%m-%d")
        current_minutes = now.hour * 60 + now.minute
        
        # 每天凌晨重置话术
        if now.hour == 0 and now.minute < 1:
            daily_msgs.clear()
            sent_today = {k for k in sent_today if k.startswith(date_key)}
        
        for hour, minute, task_type in get_schedule():
            task_key = f"{date_key}_{hour}_{minute}"
            task_minutes = hour * 60 + minute
            
            # 到点后 3 分钟内持续尝试：网络抖动/推送失败时自动重试，成功即停止，不会重复
            if task_key not in sent_today and 0 <= (current_minutes - task_minutes) <= 3:
                log(f"触发任务: {hour:02d}:{minute:02d} ({task_type})")

                if task_type not in daily_msgs:
                    weather = get_weather() if task_type in ("wake", "run") else None
                    daily_msgs[task_type] = compose_reminder(task_type, weather)
                msg = daily_msgs[task_type]
                
                if send_message(msg):
                    sent_today.add(task_key)

        
        time.sleep(30)

if __name__ == "__main__":
    main()

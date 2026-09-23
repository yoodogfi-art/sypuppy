#!/usr/bin/env python3
"""
cal2tg_v2.py - Google Calendar 일정을 텔레그램 톡방으로 쏘는 스크립트.

modes:
  test     : 텔레그램 연결 확인용 메시지 1회 발송
  chatid   : 봇이 받은 업데이트에서 chat_id 목록 출력
  digest   : 오늘(또는 --day-offset) 일정 요약 1회 발송
  reminder : 앞으로 LEAD_MINUTES 안에 시작하는 일정 알림 (중복 발송 방지)

env:
  TELEGRAM_BOT_TOKEN   (필수)
  TELEGRAM_CHAT_ID     (필수, 그룹은 보통 -100... 로 시작)
  CALENDAR_IDS         (선택, 콤마 구분. 기본 'primary')
  TIMEZONE             (선택, 기본 'Asia/Seoul')
  LEAD_MINUTES         (선택, 기본 15)
  STATE_FILE           (선택, 기본 ~/.cal2tg_state.json)
  GOOGLE_CREDENTIALS   (선택, 기본 ./credentials.json)
  GOOGLE_TOKEN         (선택, 기본 ./token.json)
"""

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CALENDAR_IDS = [c.strip() for c in os.environ.get("CALENDAR_IDS", "primary").split(",") if c.strip()]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Seoul"))
LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "15"))
STATE_FILE = os.environ.get("STATE_FILE", os.path.expanduser("~/.cal2tg_state.json"))
CRED_FILE = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN", "token.json")
 # 5240992716

# ---------- telegram ----------

def tg_api(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"telegram error: {res}")
    return res["result"]


def tg_send(text):
    return tg_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })


# ---------- google ----------

def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CRED_FILE):
                sys.exit(f"{CRED_FILE} 없음. Google Cloud Console에서 OAuth 데스크톱 클라이언트 JSON 받아서 저장.")
            flow = InstalledAppFlow.from_client_secrets_file(CRED_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def ev_start(ev):
    s = ev["start"]
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"]).astimezone(TZ), False
    return datetime.fromisoformat(s["date"]).replace(tzinfo=TZ), True


def ev_end(ev):
    e = ev["end"]
    if "dateTime" in e:
        return datetime.fromisoformat(e["dateTime"]).astimezone(TZ)
    return datetime.fromisoformat(e["date"]).replace(tzinfo=TZ)


def fetch_events(service, time_min, time_max):
    items = []
    for cid in CALENDAR_IDS:
        page = None
        while True:
            resp = service.events().list(
                calendarId=cid,
                timeMin=time_min.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                timeMax=time_max.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page,
            ).execute()
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled":
                    continue
                items.append(ev)
            page = resp.get("nextPageToken")
            if not page:
                break
    items.sort(key=lambda e: ev_start(e)[0])
    return items


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    cutoff = (datetime.now(TZ) - timedelta(days=2)).timestamp()
    state = {k: v for k, v in state.items() if v > cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------- formatting ----------

def fmt_line(ev):
    start, allday = ev_start(ev)
    title = html.escape(ev.get("summary", "(제목 없음)"))
    when = "종일" if allday else start.strftime("%H:%M")
    loc = ev.get("location")
    line = f"• <b>{when}</b>  {title}"
    if loc:
        line += f"\n   !! {html.escape(loc)}"
    return line


def fmt_reminder(ev):
    start, allday = ev_start(ev)
    title = html.escape(ev.get("summary", "(제목 없음)"))
    mins = max(0, int((start - datetime.now(TZ)).total_seconds() // 60))
    head = f"!? <b>{title}</b>"
    body = "오늘 종일 일정" if allday else f"{start.strftime('%H:%M')} 시작 ({mins}분 후)"
    out = f"{head}\n{body}"
    if ev.get("location"):
        out += f"\n!! {html.escape(ev['location'])}"
    if ev.get("hangoutLink"):
        out += f"\n~~ {ev['hangoutLink']}"
    return out


# ---------- modes ----------

def mode_test():
    tg_send("cal2tg 연결 테스트 OK")
    print("sent")


def mode_chatid():
    for u in tg_api("getUpdates", {"limit": 50}):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            print(chat.get("id"), "|", chat.get("type"), "|", chat.get("title") or chat.get("username"))


def mode_digest(day_offset=0):
    day = (datetime.now(TZ) + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
    events = fetch_events(get_service(), day, day + timedelta(days=1))
    header = f"<b>{day.strftime('%Y-%m-%d (%a)')} 일정</b>"
    if not events:
        tg_send(header + "\n일정 없음")
    else:
        tg_send(header + "\n\n" + "\n".join(fmt_line(e) for e in events))
    print(f"{len(events)} events")


def mode_reminder():
    now = datetime.now(TZ)
    window_end = now + timedelta(minutes=LEAD_MINUTES)
    events = fetch_events(get_service(), now - timedelta(minutes=1), window_end + timedelta(minutes=1))
    state = load_state()
    sent = 0
    for ev in events:
        start, allday = ev_start(ev)
        if allday:
            continue
        if not (now <= start <= window_end):
            continue
        key = f"{ev['id']}:{start.isoformat()}"
        if key in state:
            continue
        tg_send(fmt_reminder(ev))
        state[key] = now.timestamp()
        sent += 1
    save_state(state)
    print(f"{sent} sent")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["test", "chatid", "digest", "reminder"])
    p.add_argument("--day-offset", type=int, default=0)
    args = p.parse_args()

    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN 미설정")
    if args.mode != "chatid" and not CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID 미설정")

    if args.mode == "test":
        mode_test()
    elif args.mode == "chatid":
        mode_chatid()
    elif args.mode == "digest":
        mode_digest(args.day_offset)
    else:
        mode_reminder()


if __name__ == "__main__":
    main()
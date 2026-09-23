#!/usr/bin/env python3
"""
cal2tg_sa.py - Google Calendar(서비스 계정 인증) 일정을 텔레그램 톡방으로 쏘는 스크립트.

modes:
  test     : 텔레그램 연결 확인용 메시지 1회 발송
  chatid   : 봇이 받은 업데이트에서 chat_id 목록 출력
  digest   : 오늘(또는 --day-offset) 일정 요약 1회 발송
  reminder : 앞으로 LEAD_MINUTES 안에 시작하는 일정 알림 (중복 발송 방지)

env:
  TELEGRAM_BOT_TOKEN   (필수)
  TELEGRAM_CHAT_ID     (필수, 콤마로 여러 방 지정 가능. 개인=양수, 그룹=-100... 음수)
                       예: '123456789,-1001234567890'
  CALENDAR_IDS         (필수, 콤마 구분. 캘린더 설정의 '캘린더 ID' 값)
  TIMEZONE             (선택, 기본 'Asia/Seoul')
  LEAD_MINUTES         (선택, 기본 15)
  STATE_FILE           (선택, 기본 ~/.cal2tg_state.json)
  GOOGLE_SERVICE_ACCOUNT       (선택, 기본 ./service-account.json)
  GOOGLE_SERVICE_ACCOUNT_JSON  (선택, 키 파일 내용 자체. 설정 시 파일보다 우선)

사전 준비: 서비스 계정 이메일을 구글 캘린더에 '모든 일정 세부정보 보기' 권한으로 공유해야 함.
서비스 계정에는 primary 캘린더가 없으므로 CALENDAR_IDS를 반드시 지정할 것.
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
CALENDAR_IDS = [c.strip() for c in os.environ.get("CALENDAR_IDS", "").split(",") if c.strip()]
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Seoul"))
LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "15"))
STATE_FILE = os.environ.get("STATE_FILE", os.path.expanduser("~/.cal2tg_state.json"))
SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "service-account.json")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


# ---------- telegram ----------

def tg_api(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram {e.code}: {e.read().decode('utf-8', 'replace')}") from None
    if not res.get("ok"):
        raise RuntimeError(f"telegram error: {res}")
    return res["result"]


def tg_send(text):
    """등록된 모든 chat_id로 발송. 하나가 실패해도 나머지는 계속 진행."""
    results = []
    for cid in CHAT_IDS:
        try:
            results.append(tg_api("sendMessage", {
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }))
        except Exception as e:
            print(f"send failed ({cid}): {e}", file=sys.stderr)
    return results


# ---------- google ----------

def get_service():
    if SA_JSON.strip():
        info = json.loads(SA_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(SA_FILE):
        creds = service_account.Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    else:
        sys.exit(f"{SA_FILE} 없음. Cloud Console에서 서비스 계정 JSON 키를 받아 저장하거나 "
                 f"GOOGLE_SERVICE_ACCOUNT_JSON 환경변수에 내용을 넣으세요.")
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
    head = f"!! <b>{title}</b>"
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
    header = f"🗓 <b>{day.strftime('%Y-%m-%d (%a)')} 일정</b>"
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
    if args.mode in ("digest", "reminder") and not CALENDAR_IDS:
        sys.exit("CALENDAR_IDS 미설정 (구글 캘린더 설정 > 캘린더 통합 > 캘린더 ID)")
    if args.mode != "chatid" and not CHAT_IDS:
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
#!/usr/bin/env python3
"""
cal2tg_v2.py - Google Calendar -> Telegram (서비스 계정 인증)

실행 모드
  test         텔레그램 연결 확인 메시지 발송
  chatid       봇이 받은 업데이트에서 chat_id 목록 출력
  digest       오늘(또는 --day-offset) 일정 요약 발송
  reminder     LEAD_MINUTES 안에 시작하는 일정 알림 (중복 발송 방지)
  poll         밀린 텔레그램 명령어 1회 처리 후 종료 (cron/Actions용)
  run          reminder + poll 을 한 번에 (cron 권장)
  serve        롱폴링으로 명령어 상시 대기 (PC/서버 상주용, Ctrl+C 종료)
  setcommands  텔레그램 입력창 '/' 메뉴에 명령어 등록 (최초 1회)

텔레그램 명령어
  /help      명령어 목록
  /today     오늘 일정
  /tomorrow  내일 일정
  /week      앞으로 7일 일정
  /next      다음 일정 1건
  /id        이 방의 chat_id
  /ping      생존 확인

환경변수
  TELEGRAM_BOT_TOKEN            필수
  TELEGRAM_CHAT_ID              필수, 콤마로 여러 방. 개인=양수, 그룹=음수
  CALENDAR_IDS                  필수, 콤마 구분. 캘린더 설정의 '캘린더 ID'
  TIMEZONE                      선택, 기본 Asia/Seoul
  LEAD_MINUTES                  선택, 기본 15 (reminder 선행 시간, 분)
  STATE_FILE                    선택, 기본 ~/.cal2tg_state.json
  GOOGLE_SERVICE_ACCOUNT        선택, 기본 ./service-account.json
  GOOGLE_SERVICE_ACCOUNT_JSON   선택, 키 파일 내용 자체 (파일보다 우선)

사전 준비
  서비스 계정 이메일을 구글 캘린더에 '모든 일정 세부정보 보기'로 공유할 것.
  서비스 계정은 primary 캘린더가 없으므로 CALENDAR_IDS를 반드시 지정할 것.
"""

import argparse
import html
import json
import os
import sys
import time
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

COMMANDS = [
    ("help", "명령어 목록"),
    ("today", "오늘 일정"),
    ("tomorrow", "내일 일정"),
    ("week", "앞으로 7일 일정"),
    ("next", "다음 일정 1건"),
    ("id", "이 방의 chat_id"),
    ("ping", "생존 확인"),
]


# ---------- telegram ----------

def tg_api(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram {e.code}: {e.read().decode('utf-8', 'replace')}") from None
    if not res.get("ok"):
        raise RuntimeError(f"telegram error: {res}")
    return res["result"]


def tg_send(text, chat_id=None):
    """chat_id 지정 시 그 방에만, 아니면 CHAT_IDS 전체에 발송."""
    targets = [str(chat_id)] if chat_id is not None else CHAT_IDS
    out = []
    for cid in targets:
        try:
            out.append(tg_api("sendMessage", {
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }))
        except Exception as e:
            print(f"send failed ({cid}): {e}", file=sys.stderr)
    return out


# ---------- google ----------

_service = None


def get_service():
    global _service
    if _service is not None:
        return _service
    if SA_JSON.strip():
        creds = service_account.Credentials.from_service_account_info(
            json.loads(SA_JSON), scopes=SCOPES)
    elif os.path.exists(SA_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SA_FILE, scopes=SCOPES)
    else:
        sys.exit(f"{SA_FILE} 없음. 서비스 계정 JSON 키를 저장하거나 "
                 f"GOOGLE_SERVICE_ACCOUNT_JSON 환경변수에 내용을 넣으세요.")
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def ev_start(ev):
    s = ev["start"]
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"]).astimezone(TZ), False
    return datetime.fromisoformat(s["date"]).replace(tzinfo=TZ), True


def to_utc_z(dt):
    return dt.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def fetch_events(time_min, time_max):
    service = get_service()
    items = []
    for cid in CALENDAR_IDS:
        page = None
        while True:
            try:
                resp = service.events().list(
                    calendarId=cid,
                    timeMin=to_utc_z(time_min),
                    timeMax=to_utc_z(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page,
                ).execute()
            except Exception as e:
                print(f"calendar fetch failed ({cid}): {e}", file=sys.stderr)
                break
            for ev in resp.get("items", []):
                if ev.get("status") != "cancelled":
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
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", {})
    st.setdefault("offset", 0)
    return st


def save_state(st):
    cutoff = (datetime.now(TZ) - timedelta(days=2)).timestamp()
    st["sent"] = {k: v for k, v in st["sent"].items() if v > cutoff}
    d = os.path.dirname(STATE_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# ---------- formatting ----------

def esc(s):
    return html.escape(s or "")


def fmt_line(ev, with_date=False):
    start, allday = ev_start(ev)
    title = esc(ev.get("summary") or "(제목 없음)")
    when = "종일" if allday else start.strftime("%H:%M")
    if with_date:
        when = f"{start.strftime('%m/%d')} {when}"
    line = f"- <b>{when}</b>  {title}"
    if ev.get("location"):
        line += f"\n   @ {esc(ev['location'])}"
    return line


def fmt_day(day, events):
    header = f"<b>{day.strftime('%Y-%m-%d (%a)')} 일정</b>"
    if not events:
        return header + "\n일정 없음"
    return header + "\n\n" + "\n".join(fmt_line(e) for e in events)


def fmt_reminder(ev):
    start, _ = ev_start(ev)
    mins = max(0, int((start - datetime.now(TZ)).total_seconds() // 60))
    out = f"<b>{esc(ev.get('summary') or '(제목 없음)')}</b>\n{start.strftime('%H:%M')} 시작 ({mins}분 후)"
    if ev.get("location"):
        out += f"\n@ {esc(ev['location'])}"
    if ev.get("hangoutLink"):
        out += f"\n{ev['hangoutLink']}"
    return out


def day_bounds(offset=0):
    d = (datetime.now(TZ) + timedelta(days=offset)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return d, d + timedelta(days=1)


# ---------- commands ----------

def cmd_help():
    lines = ["<b>사용 가능한 명령어</b>", ""]
    lines += [f"/{name} - {desc}" for name, desc in COMMANDS]
    return "\n".join(lines)


def cmd_day(offset):
    start, end = day_bounds(offset)
    return fmt_day(start, fetch_events(start, end))


def cmd_week():
    start, _ = day_bounds(0)
    end = start + timedelta(days=7)
    events = fetch_events(start, end)
    if not events:
        return "<b>앞으로 7일</b>\n일정 없음"
    out = ["<b>앞으로 7일 일정</b>", ""]
    cur = None
    for ev in events:
        d = ev_start(ev)[0].date()
        if d != cur:
            cur = d
            out.append(f"\n<b>{d.strftime('%m/%d (%a)')}</b>")
        out.append(fmt_line(ev))
    return "\n".join(out)


def cmd_next():
    now = datetime.now(TZ)
    events = [e for e in fetch_events(now, now + timedelta(days=60))
              if ev_start(e)[0] >= now]
    if not events:
        return "예정된 일정 없음 (60일 이내)"
    ev = events[0]
    start, allday = ev_start(ev)
    delta = start - now
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, mins = divmod(rem // 60, 60)
    parts = [f"{days}일" if days else "", f"{hours}시간" if hours else "", f"{mins}분"]
    left = " ".join(p for p in parts if p)
    when = start.strftime("%m/%d") + ("" if allday else start.strftime(" %H:%M"))
    out = f"<b>{esc(ev.get('summary') or '(제목 없음)')}</b>\n{when} ({left} 후)"
    if ev.get("location"):
        out += f"\n@ {esc(ev['location'])}"
    return out


def handle_update(u):
    msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat_id = msg["chat"]["id"]
    cmd = text.split()[0].split("@")[0].lstrip("/").lower()

    if cmd == "id":
        tg_send(f"chat_id: <code>{chat_id}</code>\ntype: {msg['chat'].get('type')}", chat_id)
        return

    # /id 외 명령은 등록된 방에서만 동작
    if CHAT_IDS and str(chat_id) not in CHAT_IDS:
        return

    if cmd in ("help", "start", "command", "commands"):
        tg_send(cmd_help(), chat_id)
    elif cmd == "ping":
        tg_send(f"살아있음 ({datetime.now(TZ).strftime('%Y-%m-%d %H:%M')})", chat_id)
    elif cmd == "today":
        tg_send(cmd_day(0), chat_id)
    elif cmd == "tomorrow":
        tg_send(cmd_day(1), chat_id)
    elif cmd == "week":
        tg_send(cmd_week(), chat_id)
    elif cmd == "next":
        tg_send(cmd_next(), chat_id)
    else:
        tg_send(f"모르는 명령어: /{esc(cmd)}\n\n{cmd_help()}", chat_id)


# ---------- modes ----------

def mode_test():
    tg_send("cal2tg 연결 테스트 OK")
    print("sent")


def mode_chatid():
    for u in tg_api("getUpdates", {"limit": 100}):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat")
        if c:
            print(c.get("id"), "|", c.get("type"), "|", c.get("title") or c.get("username"))


def mode_digest(offset=0):
    start, end = day_bounds(offset)
    events = fetch_events(start, end)
    tg_send(fmt_day(start, events))
    print(f"{len(events)} events")


def mode_reminder():
    now = datetime.now(TZ)
    end = now + timedelta(minutes=LEAD_MINUTES)
    st = load_state()
    sent = 0
    for ev in fetch_events(now - timedelta(minutes=1), end + timedelta(minutes=1)):
        start, allday = ev_start(ev)
        if allday or not (now <= start <= end):
            continue
        key = f"{ev['id']}:{start.isoformat()}"
        if key in st["sent"]:
            continue
        tg_send(fmt_reminder(ev))
        st["sent"][key] = now.timestamp()
        sent += 1
    save_state(st)
    print(f"{sent} reminders")


def mode_poll():
    st = load_state()
    params = {"limit": 100, "timeout": 0}
    if st["offset"]:
        params["offset"] = st["offset"]
    updates = tg_api("getUpdates", params)
    for u in updates:
        st["offset"] = u["update_id"] + 1
        try:
            handle_update(u)
        except Exception as e:
            print(f"handler error: {e}", file=sys.stderr)
    save_state(st)
    print(f"{len(updates)} updates")


def mode_serve():
    st = load_state()
    print("serve 시작 (Ctrl+C 종료)")
    while True:
        try:
            params = {"limit": 100, "timeout": 30}
            if st["offset"]:
                params["offset"] = st["offset"]
            for u in tg_api("getUpdates", params):
                st["offset"] = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print(f"handler error: {e}", file=sys.stderr)
            save_state(st)
        except KeyboardInterrupt:
            print("\n종료")
            return
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
            time.sleep(5)


def mode_setcommands():
    tg_api("setMyCommands", {
        "commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])
    })
    print("등록 완료. 텔레그램에서 '/' 입력 시 메뉴가 뜹니다 (반영까지 몇 분 걸릴 수 있음).")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=[
        "test", "chatid", "digest", "reminder", "poll", "run", "serve", "setcommands"])
    p.add_argument("--day-offset", type=int, default=0)
    args = p.parse_args()

    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN 미설정")
    if args.mode not in ("chatid", "setcommands") and not CHAT_IDS:
        sys.exit("TELEGRAM_CHAT_ID 미설정")
    if args.mode in ("digest", "reminder", "run", "poll", "serve") and not CALENDAR_IDS:
        sys.exit("CALENDAR_IDS 미설정 (캘린더 설정 > 캘린더 통합 > 캘린더 ID)")

    if args.mode == "test":
        mode_test()
    elif args.mode == "chatid":
        mode_chatid()
    elif args.mode == "digest":
        mode_digest(args.day_offset)
    elif args.mode == "reminder":
        mode_reminder()
    elif args.mode == "poll":
        mode_poll()
    elif args.mode == "run":
        mode_reminder()
        mode_poll()
    elif args.mode == "serve":
        mode_serve()
    elif args.mode == "setcommands":
        mode_setcommands()


if __name__ == "__main__":
    main()
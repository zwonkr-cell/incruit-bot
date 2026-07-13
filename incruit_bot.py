import requests
from bs4 import BeautifulSoup
import os
import re
import html
import json
import time
import traceback
from datetime import datetime, timezone, timedelta

# 1. 설정
TG_TOKEN = os.environ.get('TG_TOKEN')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID')
TARGET_URL = "https://job.incruit.com/entry/?jobty=4&jobty=1&group1=7&compty=4&compty=10&scale=2&scale=5&scale=3&group1=17&group1=5&group1=4&group1=1&group1=3&schol=60&occ1=200&occ1=102&rgn2=18&rgn2=14&rgn2=11"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Referer': 'https://job.incruit.com/',
}

def get_jobs():
    for attempt in range(5):
        try:
            print(f"{attempt + 1}번째 인크루트 접속 시도...")
            res = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
            res.encoding = 'euc-kr'

            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                job_list = []
                items = soup.select('ul.c_row')

                for item in items:
                    try:
                        job_id = item.get('jobno')
                        company = item.select_one('.cpname').get_text(strip=True)

                        # 지역 정보 추출 (cl_md 클래스 내부의 첫 번째 span)
                        location_tag = item.select_one('.cl_md span')
                        location = location_tag.get_text(strip=True) if location_tag else "지역미정"

                        title_tag = item.select_one('.cell_mid .cl_top a')
                        title = title_tag.get_text(strip=True)
                        link = title_tag['href']
                        if not link.startswith('http'):
                            link = "https:" + link

                        info_spans = item.select('.cell_last .cl_btm span')
                        deadline = info_spans[0].get_text(strip=True) if len(info_spans) > 0 else "마감정보 없음"
                        reg_time = info_spans[1].get_text(strip=True) if len(info_spans) > 1 else ""
                        # 괄호 제거 (예: (8일전 등록) -> 8일전 등록)
                        reg_time = reg_time.replace('(', '').replace(')', '')

                        job_list.append({
                            'id': job_id,
                            'company': company,
                            'location': location,
                            'title': title,
                            'link': link,
                            'deadline': deadline,
                            'reg_time': reg_time
                        })
                    except: continue
                return job_list
            else:
                print(f"HTTP {res.status_code}")
        except Exception as e:
            print(f"에러: {e}")
        time.sleep(10)
    # 5회 모두 실패 = 사이트 접속 불가 → 예외로 알려서 오류 노티되게 함
    raise RuntimeError("인크루트 접속 5회 모두 실패(사이트 접근 불가)")

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # TG_CHAT_ID 에 콤마/공백으로 여러 명을 넣을 수 있습니다. (예: "8755814064,8467039744")
    chat_ids = [c for c in re.split(r"[,\s]+", TG_CHAT_ID or "") if c]
    for cid in chat_ids:
        data = {
            "chat_id": cid,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        res = requests.post(url, data=data, timeout=10)
        if res.status_code != 200:
            print(f"전송 실패 (chat_id={cid}): {res.text}")

# ─────────────────────────────────────────────────────────────
# 상태(bot_state.json) + 오류 노티 + 12시간 무신규 하트비트
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
STATE_FILE = "bot_state.json"
HEARTBEAT_HOURS = 12       # 이 시간 동안 새 공고가 없으면 '신규 없음' 노티
ERROR_DEDUP_HOURS = 12     # 같은 오류는 이 시간에 한 번만 노티(스팸 방지)
BOT_NAME = "인크루트봇"

def _now_iso():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def _parse_iso(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except Exception:
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("state 저장 실패:", e)

def send_plain(text):
    """모든 chat_id 에 일반텍스트 전송(오류/하트비트 노티용)."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for cid in [c for c in re.split(r"[,\s]+", TG_CHAT_ID or "") if c]:
        try:
            requests.post(url, data={"chat_id": cid, "text": text[:3900],
                                     "disable_web_page_preview": True}, timeout=10)
        except Exception as e:
            print("노티 전송 실패:", cid, e)

def notify_error(state, raw_text):
    raw_text = (raw_text or "").strip() or "알 수 없는 오류"
    sig = raw_text.splitlines()[-1][:120]
    now = datetime.now(KST)
    last_at = _parse_iso(state.get("last_error_at"))
    if state.get("last_error_sig") == sig and last_at and (now - last_at) < timedelta(hours=ERROR_DEDUP_HOURS):
        print("동일 오류 최근 노티됨 → 생략")
        return
    send_plain(f"⚠️ [{BOT_NAME}] 오류가 발생했어요 (원문):\n\n{raw_text}\n\n(발생 시각: {_now_iso()})")
    state["last_error_sig"] = sig
    state["last_error_at"] = _now_iso()

def maybe_heartbeat(state, collected, new_count):
    now = datetime.now(KST)
    last = _parse_iso(state.get("last_activity_at"))
    if last is None:
        state["last_activity_at"] = _now_iso()   # 최초: 타이머 시작(즉시 노티 방지)
        return
    if (now - last) >= timedelta(hours=HEARTBEAT_HOURS):
        send_plain(f"⏰ [{BOT_NAME}] 최근 {HEARTBEAT_HOURS}시간 내 새로 스크래핑된 채용공고가 없어요.\n"
                   f"수집완료 {collected}건, 신규후보 {new_count}건\n(확인 시각: {_now_iso()})")
        state["last_activity_at"] = _now_iso()

# ── 일일 리포트: 매일 18시(KST) 이후 첫 실행 시 전일18~당일18 신규 공고 요약 ──
REPORT_HOUR = 18
SENT_LOG_KEEP_HOURS = 48

def record_sent(state, company):
    state.setdefault("sent_log", []).append({"company": (company or "(기업명 없음)"), "at": _now_iso()})

def prune_sent_log(state, keep_hours=SENT_LOG_KEEP_HOURS):
    now = datetime.now(KST)
    kept = []
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and (now - t) <= timedelta(hours=keep_hours):
            kept.append(e)
    state["sent_log"] = kept

def maybe_daily_report(state, now=None):
    now = now or datetime.now(KST)
    today_1800 = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    today_str = now.strftime("%Y-%m-%d")
    # 최초 실행: 이미 18시 지났으면 오늘 리포트는 데이터 없어 건너뜀(다음날부터)
    if not state.get("report_initialized"):
        state["report_initialized"] = True
        if now >= today_1800:
            state["last_report_date"] = today_str
        return
    if now < today_1800 or state.get("last_report_date") == today_str:
        return
    win_start = today_1800 - timedelta(days=1)
    companies = []
    n = 0
    for e in state.get("sent_log", []):
        t = _parse_iso(e.get("at"))
        if t and win_start <= t < today_1800:
            n += 1
            c = e.get("company") or "(기업명 없음)"
            if c not in companies:
                companies.append(c)
    header = (f"📊 [{BOT_NAME}] 일일 리포트\n"
              f"({win_start.strftime('%m/%d %H:%M')} ~ {today_1800.strftime('%m/%d %H:%M')})")
    if n:
        body = (f"\n신규 공고 {n}건 · 기업 {len(companies)}곳\n\n"
                + "\n".join(f"• {c}" for c in companies))
    else:
        body = "\n이 기간에 새로 올라온 공고가 없었어요."
    send_plain(header + body)
    state["last_report_date"] = today_str
    state["last_activity_at"] = _now_iso()   # 리포트도 활동 → 하트비트 리셋

if __name__ == "__main__":
    state = load_state()
    # 일일 리포트(매일 18시 이후 첫 실행) — 수집 실패와 무관하게 먼저 처리
    try:
        maybe_daily_report(state)
    except Exception as e:
        print("일일 리포트 처리 실패:", e)
    prune_sent_log(state)
    try:
        jobs = get_jobs()
        db_file = "processed_incruit_ids.txt"
        processed_ids = open(db_file, "r").read().splitlines() if os.path.exists(db_file) else []

        new_count = 0
        new_id_list = []
        for job in reversed(jobs):
            if job['id'] not in processed_ids:
                # 공고명/회사명에 <, >, & 가 있어도 메시지가 깨지지 않도록 이스케이프
                c = html.escape(job['company'])
                t = html.escape(job['title'])
                loc = html.escape(job['location'])
                lk = html.escape(job['link'])
                dl = html.escape(job['deadline'])
                rt = html.escape(job['reg_time'])
                # 재원님이 요청하신 새로운 메시지 양식 적용
                message = (
                    f"<b>{c} - {t}</b>\n\n"
                    f"• {c}({loc})\n"
                    f"• <a href='{lk}'><b>{t}</b></a>\n"
                    f"• {dl}\n\n"
                    f"본 공고는 {rt}됐어요"
                )
                send_telegram(message)
                new_id_list.append(job['id'])
                new_count += 1
                record_sent(state, job['company'])   # 일일 리포트용 기록
                time.sleep(1.2)

        with open(db_file, "w") as f:
            f.write("\n".join((new_id_list + processed_ids)[:200]))
        print(f"완료: 수집완료 {len(jobs)}건, 신규후보 {new_count}건 발송 시도됨")

        # 활동 기록 / 12시간 무신규 하트비트
        if new_count > 0:
            state["last_activity_at"] = _now_iso()
        else:
            maybe_heartbeat(state, len(jobs), 0)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        notify_error(state, tb)   # 오류 원문을 텔레그램으로 노티(같은 오류는 12h 1회)
    finally:
        save_state(state)

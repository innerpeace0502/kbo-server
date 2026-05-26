from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import requests
import re
import os
import json as json_module
import time as time_module
import threading
import atexit
import gzip
import hashlib
import random
import schedule  # Chrome 절전 모드 스케줄러용 (매일 03:55 트리거)

app = Flask(__name__, static_folder='static')
CORS(app)

# ✅ 응답 gzip 압축 (flask-compress 사용 가능시 자동, 없으면 폴백)
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────
# 전역 캐시 (기존 변수명/구조 유지)
# ─────────────────────────────────────────
_ranking_cache      = []
_ranking_cache_time = 0
_gameinfo_cache      = {}
_gameinfo_cache_time = {}
_scores_cache      = []
_scores_cache_time = 0
_scores_cache_date = ""  # 캐시 날짜 (날짜 변경 시 만료)

# 종료 스코어 파일 영속 경로 (Railway 재시작 후에도 최종 스코어 유지)
_SCORES_PERSIST_FILE = '/tmp/kbo_scores_persist.json'
_recent_cache      = {}
_recent_cache_time = {}

# ─────────────────────────────────────────
# 내부 공유 캐시 / 동기화 객체 (신규)
# ─────────────────────────────────────────
# GameCenter 페이지 파싱 결과 (lines + stadium_map) 공유 캐시
_gc_snapshot       = {}   # {today: {'lines': [...], 'stadium_map': {...}}}
_gc_snapshot_time  = {}   # {today: ts}
_gc_snapshot_lock  = threading.Lock()

# KBO 스케줄 POST JSON 월별 캐시
_schedule_rows_cache = {}  # {(year, month): data}
_schedule_rows_time  = {}
_schedule_rows_lock  = threading.Lock()

# Selenium 드라이버 싱글톤 (RLock: _fetch_body_text 내에서 _ensure_driver 재진입)
_driver      = None
_driver_lock = threading.RLock()

# 백그라운드 프리워밍 기동 플래그
_bg_started      = False
_bg_started_lock = threading.Lock()

# 캐시 TTL (초)
_TTL_GC_SNAPSHOT = 45
_TTL_SCORES      = 120
_TTL_GAMEINFO    = 120
_TTL_RANKING     = 600
_TTL_RECENT      = 600
_TTL_SCHEDULE    = 300
_TTL_PITCHER     = 3600  # 선발투수 캐시 1시간

# 선발투수 캐시 {date_str: {away: (away_pitcher, home_pitcher)}}
_pitcher_cache      = {}
_pitcher_cache_time = {}


def _save_scores_persist(scores, date_str):
    """경기 종료 스코어를 파일에 저장 (Railway 재시작 후 복원용)."""
    try:
        with open(_SCORES_PERSIST_FILE, 'w', encoding='utf-8') as f:
            json_module.dump({'date': date_str, 'scores': scores}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[persist 저장 오류] {e}")


def _load_scores_persist():
    """파일에서 종료 스코어 복원. 날짜가 오늘이면 반환, 아니면 []."""
    try:
        if not os.path.exists(_SCORES_PERSIST_FILE):
            return []
        with open(_SCORES_PERSIST_FILE, 'r', encoding='utf-8') as f:
            data = json_module.load(f)
        if data.get('date') == get_game_date():
            return data.get('scores', [])
    except Exception as e:
        print(f"[persist 로드 오류] {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 디스크 캐시 헬퍼 (절전 모드에서 stale-but-useful 데이터 제공)
#
# 절전 모드(Chrome OFF) + 메모리 캐시 비어있을 때 사용.
# ranking(팀 순위), recent(팀별 최근 10경기)는 하루 단위로만 변하므로
# 어제 데이터라도 충분히 유용. /tmp 파일에 saved_at 타임스탬프와 함께 저장.
#
# 컨테이너 lifetime(Railway 기준 보통 1주 이상) 동안 /tmp가 유지되므로
# 재배포해도 데이터가 살아남는다.
# ─────────────────────────────────────────────────────────────────────────────

_DISK_CACHE_DIR = '/tmp'


def _disk_cache_path(name):
    return os.path.join(_DISK_CACHE_DIR, f'kbo_{name}_cache.json')


def _save_disk_cache(name, data):
    """data를 /tmp/kbo_{name}_cache.json에 저장 (timestamp 포함). 예외 무시."""
    try:
        payload = {'saved_at': time_module.time(), 'data': data}
        with open(_disk_cache_path(name), 'w', encoding='utf-8') as f:
            json_module.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[disk_cache 저장 오류 {name}] {e}")


def _load_disk_cache(name, max_age_sec=86400):
    """파일에서 캐시 복원. max_age_sec(기본 24시간) 이내면 (data, saved_at_ts) 반환,
    아니면 (None, None). 절전 모드 fallback용."""
    try:
        path = _disk_cache_path(name)
        if not os.path.exists(path):
            return None, None
        with open(path, 'r', encoding='utf-8') as f:
            payload = json_module.load(f)
        saved_at = payload.get('saved_at', 0)
        if time_module.time() - saved_at > max_age_sec:
            return None, None
        return payload.get('data'), saved_at
    except Exception as e:
        print(f"[disk_cache 로드 오류 {name}] {e}")
        return None, None


# ─────────────────────────────────────────
# 채널 정보 (원본 그대로)
# ─────────────────────────────────────────
channel_map = {
    "genie": {
        "spotv":        "51",
        "spotv2":       "52",
        "kbs_n_sports": "59",
        "mbc_sports":   "60",
        "sbs_sports":   "58",
        "kbs2":         "7",
        "mbc":          "11",
        "sbs":          "5",
    },
    "Uplus": {
        "spotv": "107", "spotv2": "108", "kbs_n_sports": "105",
        "mbc_sports": "106", "sbs_sports": "104",
        "kbs2": "7", "mbc": "11", "sbs": "5",
    },
    "btv": {
        "spotv": "986", "spotv2": "982", "kbs_n_sports": "977",
        "mbc_sports": "978", "sbs_sports": "979",
        "kbs2": "7", "mbc": "11", "sbs": "13",
    }
}

KBO_TEAMS = ["LG", "KT", "SSG", "NC", "두산", "KIA", "롯데", "삼성", "한화", "키움"]

BROADCAST_MAP = {
    "SPO-T":  "spotv",  "SPO-2T": "spotv2",
    "KN-T":   "kbs_n_sports", "MBC-SP": "mbc_sports",
    "MS-T":   "mbc_sports",   "SS-T":   "sbs_sports",
    "S-T":    "sbs",          "M-T":    "mbc",
    "K-2T":   "kbs2",         "TVING":  "tving",
}

LOGO_FILES = {
    "LG": "lg.png", "KT": "kt.png", "SSG": "ssg.png",
    "NC": "nc.png", "두산": "doosan.png", "KIA": "kia.png",
    "롯데": "lotte.png", "삼성": "samsung.png", "한화": "hanwha.png",
    "키움": "kiwoom.png"
}

CODE_TEAM = {
    'LG': 'LG', 'KT': 'KT', 'SK': 'SSG', 'NC': 'NC',
    'OB': '두산', 'HT': 'KIA', 'LT': '롯데',
    'SS': '삼성', 'HH': '한화', 'WO': '키움'
}

TEAM_CODE = {
    'LG': 'LG', 'KT': 'KT', 'SSG': 'SK', 'NC': 'NC',
    '두산': 'OB', 'KIA': 'HT', '롯데': 'LT',
    '삼성': 'SS', '한화': 'HH', '키움': 'WO'
}

STADIUMS = ['잠실', '문학', '광주', '고척', '대전', '수원', '사직', '창원', '대구', '인천', '청주', '포항', '울산']


# ─────────────────────────────────────────
# 유틸 함수
# ─────────────────────────────────────────

def get_logo_url(team):
    return f"https://web-production-6aae76.up.railway.app/logos/{team}"


def get_game_date():
    now = datetime.now(KST)
    if now.hour < 4:
        return (now - timedelta(days=1)).strftime('%Y%m%d')
    return now.strftime('%Y%m%d')


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()


def parse_teams_from_score(text):
    clean = re.sub(r'\d+', '', text)
    parts = re.split(r'vs', clean, flags=re.IGNORECASE)
    if len(parts) == 2:
        away_team = next((t for t in KBO_TEAMS if t in parts[0].strip()), None)
        home_team = next((t for t in KBO_TEAMS if t in parts[1].strip()), None)
        if away_team and home_team:
            return away_team, home_team
    return None, None


def _get_kbo_headers():
    return {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.koreabaseball.com/',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
    }


def _fmt_ts(ts):
    """캐시 갱신 시각을 HH:MM:SS로. 없으면 현재 시각."""
    if not ts:
        return datetime.now(KST).strftime('%H:%M:%S')
    return datetime.fromtimestamp(ts, KST).strftime('%H:%M:%S')


# ─────────────────────────────────────────
# Selenium 공통 헬퍼 (싱글톤 + 조건부 대기)
# ─────────────────────────────────────────

def _ensure_driver():
    """드라이버 싱글톤 보장. 호출자는 _driver_lock을 이미 보유해야 함."""
    global _driver
    if _driver is not None:
        try:
            _ = _driver.current_url
            return _driver
        except Exception:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as S

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('user-agent=Mozilla/5.0')

    for path in ['/usr/bin/chromium', '/usr/bin/chromium-browser']:
        if os.path.exists(path):
            options.binary_location = path
            break

    driver = None
    for cd in ['/usr/bin/chromedriver', '/usr/lib/chromium/chromedriver']:
        if os.path.exists(cd):
            driver = webdriver.Chrome(service=S(cd), options=options)
            break

    if driver is None:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=S(ChromeDriverManager().install()), options=options)

    _driver = driver
    return _driver


def _get_selenium_driver():
    """하위 호환용: 기존 코드가 이 이름을 import할 수도 있어 유지."""
    with _driver_lock:
        return _ensure_driver()


def _cleanup_driver():
    global _driver
    with _driver_lock:
        if _driver is not None:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None


atexit.register(_cleanup_driver)


# ─────────────────────────────────────────────────────────────────────────────
# ChromeManager: Selenium Chrome의 lifecycle을 명시적으로 관리하는 단일 책임 클래스
#
# 비유: 자동차에 비유하면 _driver는 "엔진", _driver_lock은 "키"이고,
#       ChromeManager는 "엔진을 언제 켜고 끌지 결정하는 운전기사"이다.
#       기존 엔진/키는 그대로 두고, 이 클래스가 그 위에 얇은 wrapper로 얹힌다.
#
# 이 커밋(커밋 2)에서는 클래스 정의만 추가하고 어디서도 사용하지 않는다.
# 다음 커밋에서 chrome = ChromeManager() 인스턴스를 만들어 활용한다.
# ─────────────────────────────────────────────────────────────────────────────

class ChromeManager:
    """Chrome 드라이버의 ON/OFF 상태를 추적하고 lifecycle을 관리한다.

    - start(reason): 켜진 상태로 마킹. 실제 드라이버는 _fetch_body_text에서 lazy 생성.
    - stop(reason): 기존 _cleanup_driver()를 위임 호출하여 드라이버 종료.
    - is_active(): 현재 켜진 상태인지 (API 라우트에서 분기 판단용).
    - mode(): 현재 모드 문자열 (응답에 chrome_mode로 노출용).
    """

    def __init__(self):
        self._lock = threading.RLock()  # 재진입 가능 락 (start->stop 위임 호출 대비)
        self._active = False
        self._mode = "off"

    def start(self, reason=""):
        with self._lock:
            if self._active:
                # 이미 켜져 있으면 reason만 업데이트 (모드 전환 케이스)
                if reason:
                    self._mode = reason
                return
            self._active = True
            self._mode = reason or "manual"
            print(f"[ChromeManager] ON ({reason or 'manual'}) at {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    def stop(self, reason=""):
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._mode = "off"
            # 기존 검증된 정리 함수에 위임 (RLock 재진입 OK)
            try:
                _cleanup_driver()
            except Exception as e:
                print(f"[ChromeManager] 드라이버 정리 중 예외 무시: {e}")
            print(f"[ChromeManager] OFF ({reason or 'manual'}) at {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    def is_active(self):
        with self._lock:
            return self._active

    def mode(self):
        with self._lock:
            return self._mode


class _AlwaysOnChromeManager:
    """CHROME_ALWAYS_ON=true 모드용 더미. is_active()가 항상 True.

    이 더미를 쓰면 새로 추가될 가드 로직(`if not chrome.is_active()`)이
    항상 통과하여 기존 24시간 ON 동작과 100% 동일하게 작동한다.
    긴급 롤백용 안전장치.
    """

    def start(self, reason=""):
        pass  # no-op

    def stop(self, reason=""):
        pass  # no-op (강제로 끄지 않음 - 항상 ON 유지)

    def is_active(self):
        return True

    def mode(self):
        return "always_on"


# ─────────────────────────────────────────────────────────────────────────────
# Chrome lifecycle 모드 선택
#
# 환경변수 CHROME_ALWAYS_ON으로 동작 모드를 전환한다.
#   - "true"  (기본값, 이 커밋 시점): 기존 24시간 ON 동작. _AlwaysOnChromeManager 사용.
#   - "false": 새로운 절전 모드. ChromeManager 사용 (실제 ON/OFF 분기).
#
# 이 커밋(커밋 3)에서는 chrome 인스턴스만 생성하고, 아직 어디서도 chrome.is_active()
# 같은 메서드를 호출하지 않는다. 따라서 부팅 동작은 여전히 100% 기존과 동일하다.
#
# 향후 커밋에서 API 라우트와 _bg_refresh_loop에 chrome.is_active() 가드를 추가하면,
# 그때 비로소 CHROME_ALWAYS_ON=false 모드의 효과가 발휘된다.
#
# 긴급 롤백: Railway Variables에서 CHROME_ALWAYS_ON=true 설정 → 즉시 기존 동작 복귀.
# ─────────────────────────────────────────────────────────────────────────────

_CHROME_ALWAYS_ON = os.environ.get('CHROME_ALWAYS_ON', 'false').lower() == 'true'

if _CHROME_ALWAYS_ON:
    chrome = _AlwaysOnChromeManager()
    print(f"[boot] CHROME_ALWAYS_ON=true → 기존 24시간 ON 모드 (always_on dummy)")
else:
    chrome = ChromeManager()
    print(f"[boot] CHROME_ALWAYS_ON=false → 절전 모드 (lifecycle managed)")


def _fetch_body_text(url, wait_patterns=None, max_wait=12):
    """Selenium 페이지를 가져와 body 텍스트 반환. 드라이버 재사용 + 직렬화.

    wait_patterns: 특정 문자열 패턴(하나 이상)이 body에 등장할 때까지 대기.
    고정 sleep 대신 이걸로 대기하면 평균 2~3초 단축된다.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def _do_fetch(driver):
        driver.get(url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        if wait_patterns:
            compiled = [re.compile(p) for p in wait_patterns]

            def _ready(d):
                try:
                    t = d.find_element(By.TAG_NAME, 'body').text
                except Exception:
                    return False
                return any(r.search(t) for r in compiled)

            try:
                WebDriverWait(driver, max_wait).until(_ready)
            except Exception:
                pass  # 조건 못 만나도 현재까지의 body 반환
        return driver.find_element(By.TAG_NAME, 'body').text

    with _driver_lock:
        try:
            driver = _ensure_driver()
            return _do_fetch(driver)
        except Exception as e:
            print(f"[Selenium 1차 실패] {e}")

        global _driver
        if _driver is not None:
            try:
                _driver.quit()
            except Exception:
                pass
            _driver = None
        try:
            driver = _ensure_driver()
            return _do_fetch(driver)
        except Exception as e:
            print(f"[Selenium 2차 실패] {e}")
            return None


# ─────────────────────────────────────────
# 스케줄 API 호출 (월별 캐시)
# ─────────────────────────────────────────

def _get_schedule_rows(today):
    year = today[:4]
    month = today[4:6]
    key = (year, month)
    now = time_module.time()

    with _schedule_rows_lock:
        cached = _schedule_rows_cache.get(key)
        cached_ts = _schedule_rows_time.get(key, 0)
        if cached and now - cached_ts < _TTL_SCHEDULE:
            return cached

    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': year, 'year': year,
        'month': month, 'gameMonth': month, 'teamId': ''
    }
    try:
        res = requests.post(
            'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
            headers=_get_kbo_headers(), data=data, timeout=10
        )
        result = res.json()
    except Exception as e:
        print(f"[스케줄 fetch 오류] {e}")
        with _schedule_rows_lock:
            return _schedule_rows_cache.get(key, {'rows': []})

    with _schedule_rows_lock:
        _schedule_rows_cache[key] = result
        _schedule_rows_time[key] = time_module.time()
    return result


def get_kbo_schedule(date_str):
    month = date_str[4:6]
    day   = date_str[6:8]
    target_date = f"{month}.{day}"
    try:
        result = _get_schedule_rows(date_str)
        games = []
        current_date = ""
        STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주','포항','울산']
        for row_obj in result.get('rows', []):
            row = row_obj.get('row', [])
            if not row:
                continue
            cells = [strip_html(c.get('Text', '')) for c in row]
            for cell in cells:
                if re.match(r'\d{2}\.\d{2}', cell) and len(cell) <= 8:
                    current_date = cell[:5]
                    break
            if target_date not in current_date:
                continue
            time_text = next((c for c in cells if re.match(r'\d{2}:\d{2}$', c)), '시간미정')
            away_text = home_text = ''
            for cell in cells:
                if 'vs' in cell.lower():
                    result2 = parse_teams_from_score(cell)
                    if result2[0] and result2[1]:
                        away_text, home_text = result2
                        break
            broadcast = ''
            for cell in cells:
                for code in BROADCAST_MAP:
                    if code in cell:
                        broadcast = BROADCAST_MAP[code]
                        break
                if broadcast:
                    break
            stadium_text = next((c for c in cells if any(s in c for s in STAD_LIST)), '')
            if away_text and home_text:
                games.append({
                    'time': time_text, 'away': away_text, 'home': home_text,
                    'stadium': stadium_text, 'broadcast': broadcast,
                    'away_logo': get_logo_url(away_text),
                    'home_logo': get_logo_url(home_text)
                })
        return games
    except Exception as e:
        print(f"[스케줄 오류] {e}")
        return []


def _get_today_stadium_map(today):
    """구장명 → (away팀, home팀) 매핑"""
    stadium_map = {}
    try:
        result = _get_schedule_rows(today)
        month = today[4:6]
        target_date = f"{month}.{today[6:8]}"
        current_date = ''
        STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주','포항','울산']
        for row_obj in result.get('rows', []):
            row = row_obj.get('row', [])
            for cell in row:
                if cell.get('Class') == 'day':
                    current_date = re.sub(r'<[^>]+>', '', cell.get('Text', '')).strip()[:5]
            if target_date not in current_date:
                continue
            play_cell = next((c for c in row if c.get('Class') == 'play'), None)
            if not play_cell:
                continue
            play_text = play_cell.get('Text', '')
            teams = re.findall(r'<span(?:[^>]*)>(.*?)</span>', play_text)
            teams = [t for t in teams if t and 'vs' not in t.lower()]
            if len(teams) < 2:
                continue
            away = next((t for t in KBO_TEAMS if t in teams[0]), None)
            home = next((t for t in KBO_TEAMS if t in teams[-1]), None)
            if not away or not home:
                continue

            all_text = re.sub(r'<[^>]+>', '', play_text).strip()
            for s in STAD_LIST:
                if s in all_text:
                    stadium_map[s] = (away, home)
                    break

            for cell in row:
                cell_text = re.sub(r'<[^>]+>', '', cell.get('Text', '')).strip()
                for s in STAD_LIST:
                    if s in cell_text and s not in stadium_map:
                        stadium_map[s] = (away, home)
                        break
    except Exception as e:
        print(f"[구장맵 오류] {e}")
    return stadium_map


# ─────────────────────────────────────────
# GameCenter 스냅샷 (lines + stadium_map) 공유
# ─────────────────────────────────────────

def _get_gamecenter_snapshot(today, force=False):
    global _gc_snapshot, _gc_snapshot_time
    now = time_module.time()

    if not force:
        with _gc_snapshot_lock:
            cached = _gc_snapshot.get(today)
            cached_ts = _gc_snapshot_time.get(today, 0)
            if cached and now - cached_ts < _TTL_GC_SNAPSHOT:
                return cached

    url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
    body_text = _fetch_body_text(
        url,
        wait_patterns=[r'경기예정', r'경기종료', r'\d+회[초말]']
    )
    if body_text is None:
        with _gc_snapshot_lock:
            return _gc_snapshot.get(today)

    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    stadium_map = _get_today_stadium_map(today)

    snapshot = {'lines': lines, 'stadium_map': stadium_map}
    with _gc_snapshot_lock:
        _gc_snapshot[today] = snapshot
        _gc_snapshot_time[today] = time_module.time()
    return snapshot


def _get_gamecenter_lines(today):
    """기존 이름 유지. 내부적으로는 스냅샷 라인만 반환."""
    snap = _get_gamecenter_snapshot(today)
    return snap['lines'] if snap else []


# ─────────────────────────────────────────
# 라이브 스코어 / 경기 정보 / 순위 / 최근전적
# ─────────────────────────────────────────

def get_live_scores(force=False):
    """실시간 스코어 (GameCenter 스냅샷 공유)"""
    global _scores_cache, _scores_cache_time, _scores_cache_date
    now = time_module.time()
    today = get_game_date()

    # 서버 시작 후 메모리 캐시가 비어있으면 파일에서 복원
    if not _scores_cache:
        persisted = _load_scores_persist()
        if persisted:
            _scores_cache      = persisted
            _scores_cache_date = today
            _scores_cache_time = now - _TTL_SCORES + 5  # 곧 갱신 시도하되 일단 반환 가능

    if not force and _scores_cache and now - _scores_cache_time < _TTL_SCORES and _scores_cache_date == today:
        return _scores_cache
    snap = _get_gamecenter_snapshot(today, force=force)
    if not snap:
        return _scores_cache

    lines = snap['lines']
    stadium_team_map = snap['stadium_map']

    scores = []
    try:
        i = 0
        while i < len(lines):
            line = lines[i]
            stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
            if not (stadium_match and re.search(r'\d{2}:\d{2}', line)):
                i += 1
                continue

            teams = stadium_team_map.get(stadium_match)

            try:
                if i + 2 >= len(lines):
                    i += 1
                    continue
                status_line = lines[i + 2]

                if '경기예정' in status_line:
                    if teams:
                        scores.append({
                            'away': teams[0], 'home': teams[1],
                            'away_score': '', 'home_score': '',
                            'status': '0', 'inning': ''
                        })
                    i += 6
                    continue

                # ✅ 우천취소/경기취소: status_line이 "경기취소". 6줄 블록 구조
                # (구장 / 방송사 / 경기취소 / 선OO / VS / 선OO) - 경기예정과 동일
                elif '경기취소' in status_line or '취소' in status_line:
                    if teams:
                        scores.append({
                            'away': teams[0], 'home': teams[1],
                            'away_score': '', 'home_score': '',
                            'status': '3', 'inning': '경기취소'
                        })
                    i += 6
                    continue

                elif '경기종료' in status_line:
                    vs_idx = None
                    for k in range(i+3, min(i+12, len(lines))):
                        if lines[k].strip().upper() == 'VS':
                            vs_idx = k
                            break
                    if vs_idx:
                        # "두산 5" 같은 형태에서도 끝 숫자 추출
                        def _ex(s):
                            m = re.search(r'(\d+)\s*$', s)
                            return m.group(1) if m else ''
                        away_score = _ex(lines[i+3])
                        home_score = _ex(lines[vs_idx+1]) if vs_idx+1 < len(lines) else ''
                        if away_score and home_score and teams:
                            scores.append({
                                'away': teams[0], 'home': teams[1],
                                'away_score': away_score, 'home_score': home_score,
                                'status': '2', 'inning': '경기종료'
                            })
                    i += 10
                    continue

                elif re.match(r'\d+회[초말]', status_line):
                    if i + 6 < len(lines):
                        away_score = lines[i + 3]
                        vs_line    = lines[i + 5]
                        home_score = lines[i + 6]
                        if (away_score.isdigit() and home_score.isdigit()
                                and 'VS' in vs_line.upper()):
                            if teams:
                                scores.append({
                                    'away': teams[0], 'home': teams[1],
                                    'away_score': away_score, 'home_score': home_score,
                                    'status': '1', 'inning': status_line
                                })
                                i += 8
                                continue

            except Exception as e:
                print(f"[파싱 오류] {e}")
            i += 1

    except Exception as e:
        print(f"[스코어 파싱 오류] {e}")

    if scores:
        # 종료(status=2)였던 경기를 진행 중(status=1)으로 되돌리지 않음
        # (Selenium 실패로 오래된 스냅샷 재파싱 시 status 강등 방지)
        if _scores_cache and _scores_cache_date == today:
            ended = {(s['away'], s['home']) for s in _scores_cache if s.get('status') == '2'}
            scores = [
                next((c for c in _scores_cache
                      if c['away'] == s['away'] and c['home'] == s['home']), s)
                if (s['away'], s['home']) in ended and s.get('status') != '2'
                else s
                for s in scores
            ]

        _scores_cache      = scores
        _scores_cache_time = time_module.time()
        _scores_cache_date = today

        # 경기 종료 스코어가 있으면 파일에 영속 저장 (서버 재시작 후 복원)
        if any(s.get('status') == '2' for s in scores):
            _save_scores_persist(scores, today)

        return scores
    elif _scores_cache and _scores_cache_date == today:
        # 파싱 결과 없어도 오늘 날짜 캐시가 있으면 유지
        # (경기 종료 후 게임센터에서 데이터가 사라진 경우)
        _scores_cache_time = time_module.time()  # TTL 리셋: 미갱신 시에도 재호출 폭주 방지
        return _scores_cache
    return scores


def get_game_id(today):
    game_ids = {}
    try:
        result = _get_schedule_rows(today)
        month = today[4:6]
        target_date = f"{month}.{today[6:8]}"
        current_date = ''
        for row_obj in result.get('rows', []):
            row = row_obj.get('row', [])
            for cell in row:
                if cell.get('Class') == 'day':
                    current_date = re.sub(r'<[^>]+>', '', cell.get('Text', '')).strip()[:5]
            if target_date not in current_date:
                continue
            play_cell = next((c for c in row if c.get('Class') == 'play'), None)
            if not play_cell:
                continue
            play_text = play_cell.get('Text', '')
            teams = re.findall(r'<span(?:[^>]*)>(.*?)</span>', play_text)
            teams = [t for t in teams if t and 'vs' not in t.lower()]
            if len(teams) < 2:
                continue
            away = next((t for t in KBO_TEAMS if t in teams[0]), None)
            home = next((t for t in KBO_TEAMS if t in teams[-1]), None)
            if away and home and away in TEAM_CODE and home in TEAM_CODE:
                game_id = f'{today}{TEAM_CODE[away]}{TEAM_CODE[home]}0'
                game_ids[away] = game_id
                game_ids[home] = game_id
    except Exception as e:
        print(f"[gameId 오류] {e}")
    return game_ids


def get_pitcher_from_gamecenter(today, game_id, force=False):
    """게임센터에서 투수/타자 정보 파싱 (스냅샷 공유)"""
    global _gameinfo_cache, _gameinfo_cache_time
    now = time_module.time()
    cache_key = game_id

    if not force and cache_key in _gameinfo_cache and now - _gameinfo_cache_time.get(cache_key, 0) < _TTL_GAMEINFO:
        return _gameinfo_cache[cache_key]

    try:
        snap = _get_gamecenter_snapshot(today, force=force)
        if not snap:
            return _gameinfo_cache.get(cache_key)
        lines = snap['lines']
        stadium_team_map = snap['stadium_map']
        away_code   = game_id[8:10]
        target_away = CODE_TEAM.get(away_code, '')

        i = 0
        while i < len(lines):
            line = lines[i]
            stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
            if not (stadium_match and re.search(r'\d{2}:\d{2}', line)):
                i += 1
                continue
            teams = stadium_team_map.get(stadium_match)
            if not teams or teams[0] != target_away:
                i += 1
                continue
            if i + 2 >= len(lines):
                break

            status_line = lines[i + 2]

            # 경기예정 + 우천취소(경기취소) 모두 동일 구조: i+3=어웨이선발 / i+4=VS / i+5=홈선발
            if '경기예정' in status_line or '경기취소' in status_line or '취소' in status_line:
                if i + 5 < len(lines):
                    away_raw = lines[i + 3]
                    vs_check = lines[i + 4]
                    home_raw = lines[i + 5]
                    if 'VS' in vs_check.upper():
                        is_cancelled = '취소' in status_line
                        result = {
                            'status': 'cancelled' if is_cancelled else 'pre',
                            'away_pitchers': [{'label': '선발', 'name': re.sub(r'^선', '', away_raw).strip()}],
                            'home_pitchers': [{'label': '선발', 'name': re.sub(r'^선', '', home_raw).strip()}],
                        }
                        _gameinfo_cache[cache_key] = result
                        _gameinfo_cache_time[cache_key] = time_module.time()
                        return result

            elif '경기종료' in status_line:
                vs_idx = None
                for k in range(i+3, min(i+12, len(lines))):
                    if lines[k].upper() == 'VS':
                        vs_idx = k
                        break

                away_pitchers = []
                home_pitchers = []

                # 점수 추출: lines[i+3]이 "5" 또는 "두산 5" 형태일 수 있으므로 끝 숫자 추출
                def _extract_num(s):
                    m = re.search(r'(\d+)\s*$', s)
                    return m.group(1) if m else ''

                away_score_str = _extract_num(lines[i+3]) if i+3 < len(lines) else ''
                home_score_str = ''

                if vs_idx:
                    home_score_str = _extract_num(lines[vs_idx+1]) if vs_idx+1 < len(lines) else ''
                    for k in range(i+4, vs_idx):
                        raw = lines[k]
                        if not raw or raw[0] not in ('승','패','세','홀'):
                            break
                        label = raw[0]
                        name  = raw[1:].strip()
                        away_pitchers.append({'label': label, 'name': name})
                    for k in range(vs_idx+2, min(vs_idx+6, len(lines))):
                        raw = lines[k]
                        if not raw or raw[0] not in ('승','패','세','홀'):
                            break
                        label = raw[0]
                        name  = raw[1:].strip()
                        home_pitchers.append({'label': label, 'name': name})

                result = {
                    'status': 'ended',
                    'away_score': away_score_str,
                    'home_score': home_score_str,
                    'away_pitchers': away_pitchers,
                    'home_pitchers': home_pitchers,
                }
                _gameinfo_cache[cache_key] = result
                _gameinfo_cache_time[cache_key] = time_module.time()
                return result

            elif re.match(r'\d+회[초말]', status_line):
                if i + 7 < len(lines):
                    away_raw = lines[i + 4]
                    vs_check = lines[i + 5]
                    home_raw = lines[i + 7]
                    if 'VS' in vs_check.upper():
                        is_top = '초' in status_line
                        away_clean = re.sub(r'^(승|패|홀드|세)', '', away_raw).strip()
                        home_clean = re.sub(r'^(승|패|홀드|세)', '', home_raw).strip()
                        if is_top:
                            away_p = {'label': '타', 'name': away_clean}
                            home_p = {'label': '투', 'name': home_clean}
                        else:
                            away_p = {'label': '투', 'name': away_clean}
                            home_p = {'label': '타', 'name': home_clean}
                        result = {
                            'status': 'live',
                            'away_pitchers': [away_p],
                            'home_pitchers': [home_p],
                        }
                        _gameinfo_cache[cache_key] = result
                        _gameinfo_cache_time[cache_key] = time_module.time()
                        return result
            i += 1

    except Exception as e:
        print(f"[게임센터 파싱 오류] {e}")

    return None


def get_team_ranking(force=False):
    """팀 순위 (싱글톤 드라이버 + 조건부 대기)"""
    global _ranking_cache, _ranking_cache_time
    now = time_module.time()
    if not force and _ranking_cache and now - _ranking_cache_time < _TTL_RANKING:
        return _ranking_cache

    try:
        body = _fetch_body_text(
            'https://m.koreabaseball.com/Kbo/TeamRank.aspx',
            wait_patterns=[r'(LG|KT|SSG|NC|두산|KIA|롯데|삼성|한화|키움)']
        )
        if body is None:
            return _ranking_cache

        lines = [l.strip() for l in body.split('\n') if l.strip()]
        ranking = []

        # ✅ KBO 순위 페이지가 한 줄 통합 형식으로 변경됨 (2026-05 확인):
        #   "1 삼성 43 25 17 1 0.595 - 1승"
        #   = 순위 팀명 경기 승 패 무 승률 게임차 연속
        # 게임차는 "-" 또는 "0.5"/"3" 등이므로 \S+ 로 매칭, 연속은 .+ 로.
        row_re = re.compile(
            r'^(\d+)\s+(LG|KT|SSG|NC|두산|KIA|롯데|삼성|한화|키움)'
            r'\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\S+)\s+(.+)$'
        )
        for line in lines:
            m = row_re.match(line)
            if m:
                ranking.append({
                    'rank':  m.group(1), 'team':   m.group(2),
                    'games': m.group(3), 'win':    m.group(4),
                    'lose':  m.group(5), 'draw':   m.group(6),
                    'pct':   m.group(7), 'gb':     m.group(8),
                    'streak':m.group(9),
                })

        if ranking:
            _ranking_cache = ranking
            _ranking_cache_time = time_module.time()
        return ranking

    except Exception as e:
        print(f"[순위 오류] {e}")
        return _ranking_cache


def get_recent_games(team, force=False):
    """팀 최근 10경기 결과 (teamId로 서버 사이드 필터)"""
    global _recent_cache, _recent_cache_time
    now = time_module.time()
    if not force and team in _recent_cache and now - _recent_cache_time.get(team, 0) < _TTL_RECENT:
        return _recent_cache[team]

    try:
        today = get_game_date()
        year  = today[:4]
        month = today[4:6]
        headers = _get_kbo_headers()
        results = []

        for m in [f'{int(month)-1:02d}', month]:
            if int(m) < 1:
                continue
            data = {
                'leId': '1', 'srIdList': '0,9',
                'seasonId': year, 'year': year,
                'month': m, 'gameMonth': m, 'teamId': ''
            }
            try:
                res = requests.post(
                    'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
                    headers=headers, data=data, timeout=10
                )
                rows = res.json().get('rows', [])
                for row_obj in rows:
                    row = row_obj.get('row', [])

                    # ✅ 취소 경기 제외 (우천취소, 취소 등)
                    all_row_text = ' '.join([strip_html(c.get('Text', '')) for c in row])
                    if '취소' in all_row_text or '우천' in all_row_text:
                        continue

                    # play 클래스 셀(실제 점수 셀)만 확인
                    play_cell_obj = next((c for c in row if 'play' in (c.get('Class') or '')), None)
                    if play_cell_obj:
                        game_cell = strip_html(play_cell_obj.get('Text', ''))
                        if not (team in game_cell and re.search(r'\d', game_cell) and 'vs' in game_cell.lower()):
                            continue
                    else:
                        cells = [strip_html(c.get('Text', '')) for c in row]
                        cells = [c for c in cells if c]
                        game_cell = next((c for c in cells
                            if 'vs' in c.lower() and team in c
                            and re.search(r'\d', c)), None)
                        if not game_cell:
                            continue

                    m2 = re.search(r'(.+?)(\d+)vs(\d+)(.+)', game_cell)
                    if not m2:
                        continue

                    team1  = m2.group(1).strip()
                    score1 = int(m2.group(2))
                    score2 = int(m2.group(3))

                    # ✅ 0:0 경기 제외 (경기 전 또는 취소된 경기)
                    if score1 == 0 and score2 == 0:
                        continue

                    # 양쪽에 KBO 팀명이 없으면 오파싱 방지
                    if not any(t in team1 for t in KBO_TEAMS):
                        continue
                    if not any(t in m2.group(4) for t in KBO_TEAMS):
                        continue

                    if team in team1:
                        result = '승' if score1 > score2 else ('패' if score1 < score2 else '무')
                    else:
                        result = '승' if score2 > score1 else ('패' if score2 < score1 else '무')
                    results.append(result)

            except Exception as e:
                print(f"[최근경기 월별 오류] {e}")

        recent = results[-10:] if len(results) >= 10 else results

        _recent_cache[team] = recent
        _recent_cache_time[team] = time_module.time()
        return recent

    except Exception as e:
        print(f"[최근경기 오류] {e}")
        return _recent_cache.get(team, [])


# ─────────────────────────────────────────
# 백그라운드 프리워밍 (위젯 자동갱신 체감 속도의 핵심)
# ─────────────────────────────────────────

def _games_fully_settled(scores, today):
    """오늘 일정의 모든 경기가 종료(2)/취소(3)로 확정됐는지 판정.
    - 빈 리스트면 False.
    - 파싱된 경기 수가 일정상 경기 수보다 적으면(누락) False.
      → 늦게 끝나는 경기가 파싱에서 잠깐 빠져도 '전부 종료'로 오판하지 않도록 보호.
    - 하나라도 진행중/예정이면 False.
    이 판정이 True여야만 Chrome OFF를 예약/실행한다 (최종 점수 유실 방지의 핵심).
    """
    if not scores:
        return False
    try:
        expected = len(get_kbo_schedule(today))
    except Exception:
        expected = 0
    if expected and len(scores) < expected:
        return False  # 일정보다 적게 파싱됨 = 누락 경기 있음 (아직 진행중일 수 있음)
    for s in scores:
        status = (s.get('status') or '').strip()
        inning = (s.get('inning') or '').strip()
        if status in ('2', '3'):
            continue
        if '종료' in inning or '취소' in inning or '우천' in inning:
            continue
        return False  # 하나라도 진행중/예정이면 False
    return True


def _schedule_early_stop(minutes=5):
    """모든 경기 종료 감지 시 N분 후 stop_game_mode 발사 예약 (중복 방지).
    CHROME_ALWAYS_ON=true 모드에서는 호출되지 않음 (chrome.is_active() 가드)."""
    try:
        # 이미 예약된 stop이 있으면 중복 등록 안 함
        for j in schedule.jobs:
            if 'game_stop_once' in j.tags:
                return
        target = datetime.now(KST) + timedelta(minutes=minutes)
        schedule.every().day.at(target.strftime('%H:%M'), "Asia/Seoul").do(stop_game_mode).tag('game_stop_once')
        print(f"[scheduler] 모든 경기 종료 감지 - {minutes}분 후 ({target.strftime('%H:%M')} KST) Chrome OFF 예약")
    except Exception as e:
        print(f"[scheduler] _schedule_early_stop 오류: {e}")


def _warm_caches_once():
    try:
        today = get_game_date()
        scores = get_live_scores(force=True)  # 내부에서 snapshot도 force=True로 처리
        # scores도 디스크에 저장(메모리만이던 약점 보강 — 컨테이너 재시작 후에도 살아남음)
        if scores:
            _save_disk_cache('scores', {'date': today, 'scores': scores})

        # ✅ 경기 종료 자동 감지 (CHROME_ALWAYS_ON=false 모드에서만 의미 있음)
        # 일정상 모든 경기가 종료/취소로 확정됐을 때만 OFF 예약 (누락 경기 있으면 보류)
        if not _CHROME_ALWAYS_ON and _games_fully_settled(scores, today):
            _schedule_early_stop(minutes=5)

        try:
            game_ids = get_game_id(today)
        except Exception as e:
            print(f"[prewarm game_id 오류] {e}")
            game_ids = {}

        seen = set()
        for gid in game_ids.values():
            if gid in seen:
                continue
            seen.add(gid)
            try:
                get_pitcher_from_gamecenter(today, gid, force=True)
            except Exception as e:
                print(f"[prewarm gameinfo 오류] {e}")

        try:
            ranking_data = get_team_ranking(force=True)
            if ranking_data:
                _save_disk_cache('ranking', ranking_data)  # 절전 모드 fallback용
        except Exception as e:
            print(f"[prewarm ranking 오류] {e}")

        for team in KBO_TEAMS:
            try:
                recent_data = get_recent_games(team, force=True)
                if recent_data:
                    _save_disk_cache(f'recent_{team}', recent_data)  # 팀별 디스크 저장
            except Exception as e:
                print(f"[prewarm recent {team} 오류] {e}")

        try:
            _save_pitcher_disk_cache(today)   # 선발투수 절전 fallback용
            _save_gameinfo_disk_cache(today)  # 투수(승/패/세이브)·스코어 절전 fallback용
        except Exception as e:
            print(f"[prewarm pitcher/gameinfo 오류] {e}")
    except Exception as e:
        print(f"[prewarm 사이클 오류] {e}")


def _bg_refresh_loop():
    time_module.sleep(2)  # 서버 기동 직후 혼잡 방지
    _boot_warm()  # 부팅 1회 워밍 (절전이어도 캐시를 채워 재배포 직후 데이터 공백 방지)
    while True:
        interval = 300  # 기본: 5분
        try:
            # ✅ Chrome OFF 가드: 절전 모드면 프리워밍 자체를 스킵 (메모리·CPU 절약 핵심)
            # _AlwaysOnChromeManager에서는 is_active()가 항상 True라 이 분기 통과 (기존 동작 유지)
            if not chrome.is_active():
                time_module.sleep(60)
                continue
            now_kst = datetime.now(KST)
            hour = now_kst.hour
            # 경기 시간대: KST 14시~다음날 2시
            in_game_hours = (14 <= hour <= 23) or (hour < 2)
            interval = 45 if in_game_hours else 300
            _warm_caches_once()
        except Exception as e:
            print(f"[prewarm loop 오류] {e}")
            interval = 60
        time_module.sleep(interval + random.uniform(0, 5))


def _start_background():
    global _bg_started
    if os.environ.get('WBB_DISABLE_PREWARM') == '1':
        return
    with _bg_started_lock:
        if _bg_started:
            return
        _bg_started = True
    t = threading.Thread(target=_bg_refresh_loop, daemon=True, name='wbb-prewarm')
    t.start()
    print("[prewarm] background thread started")


# ─────────────────────────────────────────────────────────────────────────────
# Chrome 절전 모드 스케줄러
#
# 매일 03:55에 morning_schedule_fetch()를 자동 실행하여 오늘 경기 일정을 받고
# 첫 경기 -2h05m에 start_game_mode, +5m에 stop_game_mode를 예약한다.
#
# CHROME_ALWAYS_ON=true에서는 _start_scheduler()가 no-op (스케줄러 미기동).
# TEST_MODE=true에서는 트리거 시각을 가까운 미래로 재배치하여 5분 안에 전 사이클 시연.
#
# 시간대 처리: schedule 라이브러리는 시스템 로컬 시간을 사용한다.
# Railway 배포 시 환경변수 TZ=Asia/Seoul 설정 필수 (그러면 컨테이너 시계가 KST가 되어
# schedule.every().day.at("03:55")가 KST 03:55에 발사).
#
# 이 커밋(커밋 4)에서는 함수들과 _start_scheduler() 호출까지 추가되지만,
# 기본값 CHROME_ALWAYS_ON=true 상태에서는 스케줄러가 기동되지 않아 부팅 동작 100% 동일.
# 절전 모드(false)로 띄우려면 환경변수를 명시적으로 설정해야 한다.
# ─────────────────────────────────────────────────────────────────────────────

_scheduler_started = False
_scheduler_started_lock = threading.Lock()


def _is_test_mode():
    """TEST_MODE=true이면 트리거 시각을 가까운 미래로 재배치 (로컬 5분 시연용)."""
    return os.environ.get('TEST_MODE', 'false').lower() == 'true'


def _parse_first_game_time(games, date_str):
    """일정에서 가장 빠른 경기 시작 시각(datetime, KST). 없으면 None."""
    if not games:
        return None
    year, month, day = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    earliest = None
    for g in games:
        t = (g.get('time') or '').strip()
        if not re.match(r'^\d{1,2}:\d{2}$', t):
            continue
        hh, mm = map(int, t.split(':'))
        dt = datetime(year, month, day, hh, mm, tzinfo=KST)
        if earliest is None or dt < earliest:
            earliest = dt
    return earliest


def _estimate_last_game_end(games, date_str):
    """일정에서 가장 늦은 경기 시작 시각 + 5시간 30분. 없으면 None.
    (4시간은 짧아 연장·고득점 경기 진행 중 Chrome이 꺼지는 문제가 있었음.
     이 값은 OFF '안전망' 시각일 뿐, 실제 OFF는 stop_game_mode가 미종료 경기를 재확인해 결정.)"""
    if not games:
        return None
    year, month, day = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    latest = None
    for g in games:
        t = (g.get('time') or '').strip()
        if not re.match(r'^\d{1,2}:\d{2}$', t):
            continue
        hh, mm = map(int, t.split(':'))
        dt = datetime(year, month, day, hh, mm, tzinfo=KST)
        if latest is None or dt > latest:
            latest = dt
    return latest + timedelta(hours=5, minutes=30) if latest else None


def morning_schedule_fetch():
    """매일 03:55 트리거. 오늘 일정 받고 게임 모드 예약 후 Chrome OFF.
    휴식일(경기 없음)은 즉시 OFF하고 다음 날 03:55까지 대기."""
    try:
        today = get_game_date()
        chrome.start("morning_schedule_fetch")
        # 일정은 _get_schedule_rows()로 받지만 Selenium 사용 안 함 (일반 HTTP)
        games = get_kbo_schedule(today)

        # ── 순위·선발 디스크 캐시 새벽 갱신 (절전 모드 fallback용) ──
        # game_mode가 꺼진 아침~낮 시간대에도 위젯에 순위/선발이 보이도록 매일 최신화.
        try:
            ranking_data = get_team_ranking(force=True)
            if ranking_data:
                _save_disk_cache('ranking', ranking_data)
                print(f"[scheduler] 순위 디스크 캐시 갱신 ({len(ranking_data)}팀)")
        except Exception as e:
            print(f"[scheduler] morning 순위 갱신 오류: {e}")
        try:
            # 선발은 오늘(달력 기준) 낮에 열릴 경기를 받는다 (get_game_date는 03:55에 '어제'를 가리킴)
            _save_pitcher_disk_cache(datetime.now(KST).strftime('%Y%m%d'))
        except Exception as e:
            print(f"[scheduler] morning 선발 갱신 오류: {e}")

        if not games:
            print(f"[scheduler] {today} 경기 없음 - 휴식일, 다음 03:55까지 대기")
        else:
            first_start = _parse_first_game_time(games, today)
            last_end = _estimate_last_game_end(games, today)
            if first_start and last_end:
                _schedule_game_mode(first_start, last_end)
                start_at = (first_start - timedelta(hours=2, minutes=5)).strftime('%H:%M')
                stop_at = (last_end + timedelta(minutes=5)).strftime('%H:%M')
                print(f"[scheduler] 게임 모드 예약: ON {start_at} (첫경기 -2h05m) / OFF {stop_at} (마지막 +5m)")
            else:
                print(f"[scheduler] {today} 경기 시각 파싱 실패 - 게임 모드 예약 생략")
    except Exception as e:
        print(f"[scheduler] morning_schedule_fetch 오류: {e}")
    finally:
        chrome.stop("morning_schedule_fetch_done")


def start_game_mode():
    """첫 경기 -2h05m 트리거. Chrome ON 마킹. _bg_refresh_loop가 활성화됨."""
    chrome.start("game_mode")
    print(f"[scheduler] 게임 모드 시작 - 실시간 스크래핑 ON")
    return schedule.CancelJob  # 일회성 (다음 날 morning_schedule_fetch가 재예약)


def stop_game_mode():
    """마지막 경기 +여유시간 또는 종료 감지 후 트리거. Chrome OFF + 다음 날 준비.
    단, 일정상 아직 안 끝난 경기가 있으면 OFF를 20분 미루고 재확인한다 (최종 점수 유실 방지).
    새벽 02:00~03:59에는 마라톤 경기라도 무조건 OFF (03:55 morning fetch가 사이클 재시작)."""
    today = get_game_date()
    now_kst = datetime.now(KST)
    past_hard_cap = 2 <= now_kst.hour < 4  # 02:00~03:59 KST → 무조건 OFF
    if not past_hard_cap:
        try:
            scores = get_live_scores(force=True)  # OFF 직전 마지막 강제 갱신
            if not _games_fully_settled(scores, today):
                target = now_kst + timedelta(minutes=20)
                schedule.clear('game_stop_once')
                schedule.every().day.at(target.strftime('%H:%M'), "Asia/Seoul").do(stop_game_mode).tag('game_stop_once')
                print(f"[scheduler] 미종료 경기 감지 - Chrome OFF를 {target.strftime('%H:%M')} KST로 연기")
                return schedule.CancelJob
        except Exception as e:
            print(f"[scheduler] stop_game_mode 종료확인 오류: {e}")
    chrome.stop("game_mode_end")
    print(f"[scheduler] 게임 모드 종료 - Chrome OFF")
    _reschedule_next_morning()
    return schedule.CancelJob


def _schedule_game_mode(first_game_kst, last_game_kst):
    """game_start/stop 시각을 schedule에 등록 (일회성, 태그 기반, KST 기준)."""
    start_at = (first_game_kst - timedelta(hours=2, minutes=5)).strftime('%H:%M')
    stop_at = (last_game_kst + timedelta(minutes=5)).strftime('%H:%M')
    schedule.clear('game_start_once')
    schedule.clear('game_stop_once')
    # ✅ "Asia/Seoul" 인자로 KST 명시 (시스템 로컬 시간이 UTC여도 KST 기준 동작)
    schedule.every().day.at(start_at, "Asia/Seoul").do(start_game_mode).tag('game_start_once')
    schedule.every().day.at(stop_at, "Asia/Seoul").do(stop_game_mode).tag('game_stop_once')


def _reschedule_next_morning():
    """일회성 game_* 태그를 정리. 다음 날 03:55 morning_schedule_fetch는
    영구 등록되어 있어 자동으로 재발사된다."""
    schedule.clear('game_start_once')
    schedule.clear('game_stop_once')


def _scheduler_loop():
    """schedule.run_pending()을 주기적으로 호출하는 워커 스레드.
    TEST_MODE면 1초마다, 평상시 20초마다 체크."""
    time_module.sleep(2)
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"[scheduler loop 오류] {e}")
        time_module.sleep(1 if _is_test_mode() else 20)


def _start_scheduler():
    """스케줄러 기동. CHROME_ALWAYS_ON=true면 no-op.
    WBB_DISABLE_PREWARM은 prewarm 전용 플래그라 여기서는 무시 (스케줄러는 별개)."""
    global _scheduler_started
    if _CHROME_ALWAYS_ON:
        print("[scheduler] CHROME_ALWAYS_ON=true → 스케줄러 비활성 (기존 동작 유지)")
        return
    if os.environ.get('WBB_DISABLE_SCHEDULER') == '1':
        # 스케줄러 전용 비활성화 (운영 비상시 토글용, 기본은 활성)
        print("[scheduler] WBB_DISABLE_SCHEDULER=1 → 스케줄러 비활성")
        return
    with _scheduler_started_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    # ✅ schedule.every().day.at(..., "Asia/Seoul")로 KST 명시
    #    시스템 로컬 시간(보통 UTC)이 아닌 KST 기준으로 트리거 발사 보장
    if _is_test_mode():
        # 테스트: 가까운 미래로 트리거 시각 재배치 (시계 자체를 건드리지 않음)
        now = datetime.now(KST)
        t1 = (now + timedelta(minutes=1)).strftime('%H:%M')
        t2 = (now + timedelta(minutes=2)).strftime('%H:%M')
        t3 = (now + timedelta(minutes=5)).strftime('%H:%M')
        schedule.every().day.at(t1, "Asia/Seoul").do(morning_schedule_fetch).tag('test')
        schedule.every().day.at(t2, "Asia/Seoul").do(start_game_mode).tag('test')
        schedule.every().day.at(t3, "Asia/Seoul").do(stop_game_mode).tag('test')
        print(f"[TEST_MODE] schedule_fetch={t1} game_start={t2} game_stop={t3} (KST 기준)")
    else:
        schedule.every().day.at("03:55", "Asia/Seoul").do(morning_schedule_fetch).tag('morning')
        print(f"[scheduler] 매일 03:55 KST에 morning_schedule_fetch 예약 (시간대 명시: Asia/Seoul)")

    # 부팅 시점 시간대 진단 정보 출력
    import time as _t
    sys_tz = _t.tzname
    sys_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    kst_now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[scheduler diag] system tzname={sys_tz}, system_now={sys_now}, kst_now={kst_now}")
    print(f"[scheduler diag] registered jobs: {len(schedule.jobs)}")
    for j in schedule.jobs:
        print(f"  - tags={j.tags}, next_run={j.next_run}")

    t = threading.Thread(target=_scheduler_loop, daemon=True, name='kbo-scheduler')
    t.start()
    print("[scheduler] background thread started")


# ─────────────────────────────────────────
# Flask 응답 가속 (Gzip + ETag/304)
# ─────────────────────────────────────────

@app.after_request
def _optimize_response(response):
    try:
        if (response.status_code == 200
                and response.direct_passthrough is False
                and response.mimetype
                and (response.mimetype.startswith('application/json')
                     or response.mimetype.startswith('text/'))):
            data = response.get_data()
            if data:
                etag = hashlib.md5(data).hexdigest()
                response.set_etag(etag)
                if request.if_none_match and etag in request.if_none_match:
                    response.status_code = 304
                    response.set_data(b'')
                    return response

                accept = request.headers.get('Accept-Encoding', '')
                if ('gzip' in accept
                        and len(data) > 200
                        and 'Content-Encoding' not in response.headers):
                    gzipped = gzip.compress(data)
                    response.set_data(gzipped)
                    response.headers['Content-Encoding'] = 'gzip'
                    response.headers['Content-Length'] = str(len(gzipped))
                    response.headers.add('Vary', 'Accept-Encoding')
    except Exception as e:
        print(f"[after_request 오류] {e}")
    return response


# ─────────────────────────────────────────
# Flask 라우트 (경로/응답 스키마 모두 기존 그대로)
# ─────────────────────────────────────────

@app.route('/')
def home():
    return jsonify({'상태': '서버 정상 작동중!', '시간': datetime.now(KST).strftime('%Y-%m-%d %H:%M')})


@app.route('/webapp')
def webapp():
    from flask import Response
    import os
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index_webapp.html'),
        '/app/index_webapp.html',
        os.path.join(os.getcwd(), 'index_webapp.html'),
        'index_webapp.html',
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return Response(f.read(), mimetype='text/html')
    return Response('index_webapp.html not found', status=404)


@app.route('/logos/<team>')
def get_logo(team):
    from flask import make_response
    filename = LOGO_FILES.get(team)
    if filename:
        png_path = os.path.join('static', 'logos', filename)
        if os.path.exists(png_path):
            resp = make_response(send_from_directory('static/logos', filename))
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp

    from PIL import Image, ImageDraw, ImageFont
    import io

    team_colors = {
        "LG": (195,4,82), "KT": (227,30,38), "SSG": (206,14,45),
        "NC": (7,29,73),  "두산": (19,18,48), "KIA": (234,0,41),
        "롯데": (4,30,66), "삼성": (0,85,168), "한화": (255,102,0),
        "키움": (130,0,36)
    }
    color = team_colors.get(team, (68,68,68))
    size  = 120
    img   = Image.new('RGBA', (size, size), (0,0,0,0))
    draw  = ImageDraw.Draw(img)
    draw.ellipse([0,0,size-1,size-1], fill=color)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    text = team[:2]
    bbox = draw.textbbox((0,0), text, font=font)
    tw = bbox[2]-bbox[0]; th = bbox[3]-bbox[1]
    draw.text(((size-tw)//2,(size-th)//2), text, fill=(255,255,255), font=font)
    img_data = io.BytesIO()
    img.save(img_data, format='PNG')
    img_data.seek(0)
    resp = make_response(send_file(img_data, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Chrome 절전 모드용 응답 헬퍼
#
# Chrome OFF 상태에서 API 호출이 오면:
#   1) 캐시가 있으면 캐시 + cached_at(언제 캐시되었는지) 반환
#   2) 캐시도 없으면 빈 결과 + 200 OK 반환 (안드로이드 앱 크래시 방지)
#
# 기존 응답 키(scores, ranking, updated 등)는 유지하고
# 새 키(cached_at, note, chrome_mode)만 추가하여 하위 호환성 확보.
# ─────────────────────────────────────────────────────────────────────────────

def _iso_kst(ts):
    """Unix 타임스탬프를 ISO 8601 KST 문자열로. None/0이면 None."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, KST).isoformat()
    except Exception:
        return None


def _empty_payload(kind):
    """Chrome OFF + 캐시 없음 상태의 표준 빈 응답.
    안드로이드 앱 호환을 위해 기존 응답의 모든 키를 빈 값으로 채워서 반환."""
    base = {
        'cached_at': None,
        'note': '서버 절전 모드 - 경기 시간대에 다시 시도해주세요',
        'chrome_mode': chrome.mode(),
        'updated': '',
    }
    if kind == 'scores':
        base['scores'] = []
    elif kind == 'ranking':
        base['ranking'] = []
    elif kind == 'recent':
        base['recent'] = []
        base['team'] = ''
    elif kind == 'gameinfo':
        base.update({
            'game_id': '', 'away': '', 'home': '',
            'status': 'off',
            'away_score': '', 'home_score': '',
            'away_pitchers': [], 'home_pitchers': [],
        })
    elif kind == 'pitcher':
        base['pitchers'] = []
    return base


def _attach_pitcher_info(date_str, games):
    """게임 목록에 선발투수 정보 추가 (캐시 적용, 경기 시작 후 스킵)"""
    global _pitcher_cache, _pitcher_cache_time
    now_ts = time_module.time()

    # 현재 KST 시간
    now_kst = datetime.now(KST)
    now_hhmm = now_kst.hour * 60 + now_kst.minute  # 분 단위

    try:
        game_ids = get_game_id(date_str)
        for game in games:
            away = game['away']
            cache_key = f"{date_str}_{away}"

            # ✅ 경기 시작 시간 파싱 (예: "18:30")
            game_time_str = game.get('time', '')
            try:
                gh, gm = map(int, game_time_str.split(':'))
                game_hhmm = gh * 60 + gm
            except Exception:
                game_hhmm = 18 * 60 + 30  # 파싱 실패 시 기본값

            # ✅ 경기 시작 10분 이후면 투수 정보 스킵 (Selenium 호출 안 함)
            if now_hhmm >= game_hhmm + 10:
                game['away_pitcher'] = ''
                game['home_pitcher'] = ''
                continue

            # ✅ 경기 시작 1시간 전부터 TTL 5분, 그 외 1시간
            ttl = 300 if now_hhmm >= game_hhmm - 60 else _TTL_PITCHER

            # ✅ 캐시 히트 → 즉시 반환
            if cache_key in _pitcher_cache and \
               now_ts - _pitcher_cache_time.get(cache_key, 0) < ttl:
                game['away_pitcher'], game['home_pitcher'] = _pitcher_cache[cache_key]
                continue

            game_id = game_ids.get(away)
            if game_id:
                info = get_pitcher_from_gamecenter(date_str, game_id)
                # 경기예정(pre) + 우천취소(cancelled) 모두 선발투수 표시
                if info and info.get('status') in ('pre', 'cancelled'):
                    pa = info.get('away_pitchers', [])
                    ph = info.get('home_pitchers', [])
                    ap = pa[0]['name'] if pa else ''
                    hp = ph[0]['name'] if ph else ''
                else:
                    ap, hp = '', ''
            else:
                ap, hp = '', ''

            game['away_pitcher'] = ap
            game['home_pitcher'] = hp
            # 캐시 저장
            _pitcher_cache[cache_key] = (ap, hp)
            _pitcher_cache_time[cache_key] = now_ts

    except Exception as e:
        print(f"[선발투수 첨부 오류] {e}")
        for game in games:
            game.setdefault('away_pitcher', '')
            game.setdefault('home_pitcher', '')
    return games


def _save_pitcher_disk_cache(date_str):
    """date_str 경기의 선발투수 전체를 디스크에 저장 (절전 모드 fallback용).
    선발 이름이 하나라도 있을 때만 저장 → 미발표/경기중(빈값)일 땐 기존 캐시 보존."""
    try:
        games = get_kbo_schedule(date_str)
        if not games:
            return
        _attach_pitcher_info(date_str, games)
        result = [{'date': date_str,
                   'away': g['away'], 'home': g['home'],
                   'away_pitcher': g.get('away_pitcher', ''),
                   'home_pitcher': g.get('home_pitcher', '')} for g in games]
        if any(p['away_pitcher'] or p['home_pitcher'] for p in result):
            _save_disk_cache('pitcher', result)
    except Exception as e:
        print(f"[pitcher disk 저장 오류 {date_str}] {e}")


def _save_gameinfo_disk_cache(date_str):
    """경기별 gameinfo(스코어/투수)를 디스크에 저장 (절전 모드 투수 정보 fallback용)."""
    try:
        game_ids = get_game_id(date_str)
        seen = set()
        result = []
        for gid in game_ids.values():
            if gid in seen:
                continue
            seen.add(gid)
            gc = get_pitcher_from_gamecenter(date_str, gid)
            if gc:
                result.append({
                    'game_id': gid,
                    'away': CODE_TEAM.get(gid[8:10], gid[8:10]),
                    'home': CODE_TEAM.get(gid[10:12], gid[10:12]),
                    'status': gc.get('status', ''),
                    'away_score': gc.get('away_score', ''),
                    'home_score': gc.get('home_score', ''),
                    'away_pitchers': gc.get('away_pitchers', []),
                    'home_pitchers': gc.get('home_pitchers', []),
                })
        if result:
            _save_disk_cache('gameinfo_all', result)
    except Exception as e:
        print(f"[gameinfo disk 저장 오류 {date_str}] {e}")


def _boot_warm():
    """컨테이너 부팅 시 1회 데이터 워밍 + game_mode 재예약.

    재배포로 /tmp·메모리 캐시·schedule 잡이 비어 순위/투수/최근경기가 안 뜨고
    라이브 갱신이 멈추던 공백을 방지한다.

    - 기본: Chrome을 잠깐 켜 데이터를 받아 캐시·디스크에 저장한 뒤 다시 끈다.
    - 부팅 시점이 game_mode 시간대(첫경기 -2h05m ~ 마지막 +5m)이면 Chrome을 유지하고
      game_mode를 재예약해 라이브 갱신을 즉시 복구한다.
      (게임 도중 재시작이 라이브를 끊지 않게 하는 핵심 가드.)"""
    if _CHROME_ALWAYS_ON:
        return  # 항상 ON이면 _bg_refresh_loop가 처리
    keep_chrome = False
    try:
        chrome.start("boot_warm")
        today = get_game_date()
        try:
            scores = get_live_scores(force=True)
            if scores:
                _save_disk_cache('scores', {'date': today, 'scores': scores})
        except Exception as e:
            print(f"[boot_warm scores 오류] {e}")
        try:
            rk = get_team_ranking(force=True)
            if rk:
                _save_disk_cache('ranking', rk)
        except Exception as e:
            print(f"[boot_warm ranking 오류] {e}")
        for t in KBO_TEAMS:
            try:
                rc = get_recent_games(t, force=True)
                if rc:
                    _save_disk_cache(f'recent_{t}', rc)
            except Exception as e:
                print(f"[boot_warm recent {t} 오류] {e}")
        _save_pitcher_disk_cache(today)
        _save_gameinfo_disk_cache(today)

        # game_mode 재예약 — 재시작으로 잃은 schedule 잡 복구.
        # 부팅이 game_mode 시간대 안이면 chrome을 유지해 _bg가 _warm을 이어가게 한다.
        try:
            games = get_kbo_schedule(today)
            if games:
                first_start = _parse_first_game_time(games, today)
                last_end = _estimate_last_game_end(games, today)
                if first_start and last_end:
                    _schedule_game_mode(first_start, last_end)
                    now_kst = datetime.now(KST)
                    win_start = first_start - timedelta(hours=2, minutes=5)
                    win_end = last_end + timedelta(minutes=5)
                    if win_start <= now_kst <= win_end:
                        keep_chrome = True
                        print(f"[boot_warm] game_mode 시간대({win_start.strftime('%H:%M')}~{win_end.strftime('%H:%M')}) — Chrome 유지")
        except Exception as e:
            print(f"[boot_warm game_mode 재예약 오류] {e}")

        print("[boot_warm] 부팅 워밍 완료")
    except Exception as e:
        print(f"[boot_warm 오류] {e}")
    finally:
        if not keep_chrome:
            chrome.stop("boot_warm_done")


def _is_all_games_started_long_ago(games, base_date, hours_after_start=4):
    """일정의 모든 경기가 'base_date 시각 + hours_after_start' 이전에 시작되었는지.
    True면 모든 경기가 이미 끝났다고 추정 가능."""
    if not games:
        return False
    try:
        now_kst = datetime.now(KST)
        latest_start = None
        for g in games:
            t = (g.get('time') or '').strip()
            if not re.match(r'^\d{1,2}:\d{2}$', t):
                continue
            hh, mm = map(int, t.split(':'))
            dt = datetime(base_date.year, base_date.month, base_date.day, hh, mm, tzinfo=KST)
            if latest_start is None or dt > latest_start:
                latest_start = dt
        if latest_start and now_kst > latest_start + timedelta(hours=hours_after_start):
            return True
    except Exception:
        pass
    return False


@app.route('/api/schedule/today')
def today_schedule():
    team      = request.args.get('team')
    today_str = get_game_date()
    today     = datetime.strptime(today_str, '%Y%m%d')
    games     = get_kbo_schedule(today_str)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]

    # ✅ 오늘 경기가 아예 없을 때만 다음 경기 찾기 (최대 7일).
    # get_game_date()가 04:00 컷오프라 자정~04:00 사이엔 어제 게임 날짜를 유지하므로,
    # 오늘 경기가 다 끝났어도 04:00이 지나기 전까진 결과를 그대로 보여준다.
    if not games:
        for delta in range(1, 8):
            next_date = today + timedelta(days=delta)
            next_str = next_date.strftime('%Y%m%d')
            next_games = get_kbo_schedule(next_str)
            if team:
                next_games = [g for g in next_games if team in g['away'] or team in g['home']]
            if next_games:
                return jsonify({
                    '날짜': next_date.strftime('%Y-%m-%d'),
                    '경기목록': next_games,
                    '경기수': len(next_games),
                    '내일경기': delta == 1,
                    '다음경기': delta > 1,
                })
        # 7일 안에 경기 없으면 빈 응답
        return jsonify({
            '날짜': today.strftime('%Y-%m-%d'),
            '경기목록': [],
            '경기수': 0,
            'note': '향후 7일간 예정 경기 없음'
        })

    return jsonify({'날짜': today.strftime('%Y-%m-%d'), '경기목록': games, '경기수': len(games)})


@app.route('/api/pitcher/today')
def pitcher_today():
    """선발투수 전용 API — 무거운 Selenium 호출을 schedule API와 분리"""
    team      = request.args.get('team')
    # Chrome OFF 가드: Selenium 호출 대신 디스크 캐시 fallback.
    # 선발은 경기 전 확정 정보라 절전 시간대(낮)에도 캐시로 보여줄 수 있어야 한다.
    # 단, 디스크 캐시는 24h 유효이므로 어제 데이터를 오늘 보여주지 않도록 date 비교로 거른다.
    if not chrome.is_active():
        disk_data, disk_ts = _load_disk_cache('pitcher')
        if disk_data:
            today_str = get_game_date()
            result = [p for p in disk_data if p.get('date') == today_str]
            if team:
                result = [p for p in result if team in p['away'] or team in p['home']]
            if result:
                return jsonify({
                    'pitchers': result,
                    'cached_at': _iso_kst(disk_ts),
                    'note': '디스크 캐시 (서버 절전 모드)',
                    'chrome_mode': chrome.mode(),
                })
        return jsonify(_empty_payload('pitcher'))
    today_str = get_game_date()
    today     = datetime.strptime(today_str, '%Y%m%d')
    games     = get_kbo_schedule(today_str)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]

    if not games:
        tomorrow_str = (today + timedelta(days=1)).strftime('%Y%m%d')
        games        = get_kbo_schedule(tomorrow_str)
        if team:
            games = [g for g in games if team in g['away'] or team in g['home']]
        games = _attach_pitcher_info(tomorrow_str, games)
    else:
        games = _attach_pitcher_info(today_str, games)

    result = [{'away': g['away'], 'home': g['home'],
               'away_pitcher': g.get('away_pitcher', ''),
               'home_pitcher': g.get('home_pitcher', '')} for g in games]
    return jsonify({'pitchers': result})


@app.route('/api/schedule/<date>')
def schedule_by_date(date):
    team = request.args.get('team')
    try:
        d = datetime.strptime(date, '%Y%m%d')
    except Exception:
        return jsonify({'오류': '날짜 형식은 YYYYMMDD 입니다'}), 400
    games = get_kbo_schedule(date)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]
    return jsonify({'날짜': d.strftime('%Y-%m-%d'), '경기목록': games, '경기수': len(games)})


@app.route('/api/teams')
def teams():
    return jsonify({'팀목록': KBO_TEAMS})


@app.route('/api/channel')
def channel():
    iptv        = request.args.get('iptv')
    broadcaster = request.args.get('broadcaster')
    if not iptv or not broadcaster:
        return jsonify({'오류': 'iptv와 broadcaster를 입력해주세요'}), 400
    ch = channel_map.get(iptv, {}).get(broadcaster, '정보없음')
    return jsonify({'iptv': iptv, 'broadcaster': broadcaster, '채널번호': ch})


@app.route('/api/scores')
def live_scores():
    team   = request.args.get('team')
    # Chrome OFF 가드: 절전 모드면 캐시만 반환 (Selenium 호출 금지)
    if not chrome.is_active():
        today = get_game_date()
        # 1순위: 메모리 캐시
        if _scores_cache and _scores_cache_date == today:
            cached = _scores_cache
            if team:
                cached = [s for s in cached if team in s['away'] or team in s['home']]
            return jsonify({
                'scores': cached,
                'updated': _fmt_ts(_scores_cache_time),
                'cached_at': _iso_kst(_scores_cache_time),
                'note': '캐시된 결과 (서버 절전 모드)',
                'chrome_mode': chrome.mode(),
            })
        # 2순위: 디스크 캐시 (컨테이너 재시작으로 메모리가 비어도 복구 가능)
        disk, ts = _load_disk_cache('scores')
        if disk and isinstance(disk, dict) and disk.get('date') == today:
            cached = disk.get('scores', [])
            if team:
                cached = [s for s in cached if team in s['away'] or team in s['home']]
            return jsonify({
                'scores': cached,
                'updated': _fmt_ts(ts),
                'cached_at': _iso_kst(ts),
                'note': '디스크 캐시 (서버 절전 모드)',
                'chrome_mode': chrome.mode(),
            })
        return jsonify(_empty_payload('scores'))
    scores = get_live_scores()
    if team:
        scores = [s for s in scores if team in s['away'] or team in s['home']]
    return jsonify({'scores': scores, 'updated': _fmt_ts(_scores_cache_time)})


@app.route('/api/gameinfo')
def game_info():
    team  = request.args.get('team', '')
    today = get_game_date()

    # Chrome OFF 가드: 디스크 캐시(gameinfo_all)에서 team 경기를 찾아 반환.
    # 단, game_id 앞 8자리(yyyyMMdd)가 오늘과 같을 때만 — 어제 데이터를 오늘 표시하지 않도록.
    if not chrome.is_active():
        disk, ts = _load_disk_cache('gameinfo_all')
        if disk:
            today_str = get_game_date()
            match = next(
                (g for g in disk
                 if g.get('game_id', '')[:8] == today_str
                    and ((not team) or team in (g.get('away'), g.get('home')))),
                None,
            )
            if match:
                return jsonify({
                    'game_id':       match.get('game_id', ''),
                    'away':          match.get('away', ''),
                    'home':          match.get('home', ''),
                    'status':        match.get('status', ''),
                    'away_score':    match.get('away_score', ''),
                    'home_score':    match.get('home_score', ''),
                    'away_pitchers': match.get('away_pitchers', []),
                    'home_pitchers': match.get('home_pitchers', []),
                    'updated':       _fmt_ts(ts),
                    'cached_at':     _iso_kst(ts),
                    'note':          '디스크 캐시 (서버 절전 모드)',
                    'chrome_mode':   chrome.mode(),
                })
        return jsonify(_empty_payload('gameinfo'))

    game_ids = get_game_id(today)
    if not game_ids:
        return jsonify({'error': '오늘 경기 없음'}), 404

    if team and team in game_ids:
        game_id = game_ids[team]
    else:
        game_id = list(game_ids.values())[0]

    away_code = game_id[8:10]
    home_code = game_id[10:12]
    away_name = CODE_TEAM.get(away_code, away_code)
    home_name = CODE_TEAM.get(home_code, home_code)

    gc = get_pitcher_from_gamecenter(today, game_id)
    updated_ts = _gameinfo_cache_time.get(game_id, 0)
    if not gc:
        return jsonify({
            'game_id': game_id, 'away': away_name, 'home': home_name,
            'status': 'unknown',
            'away_pitchers': [], 'home_pitchers': [],
            'updated': _fmt_ts(updated_ts)
        })

    return jsonify({
        'game_id':      game_id,
        'away':         away_name,
        'home':         home_name,
        'status':       gc['status'],
        'away_score':   gc.get('away_score', ''),
        'home_score':   gc.get('home_score', ''),
        'away_pitchers': gc.get('away_pitchers', []),
        'home_pitchers': gc.get('home_pitchers', []),
        'updated':      _fmt_ts(updated_ts)
    })


@app.route('/api/ranking')
def team_ranking():
    # Chrome OFF 가드: 순위는 하루 단위로 천천히 변하므로 캐시 적극 재사용
    if not chrome.is_active():
        # 1순위: 메모리 캐시
        if _ranking_cache:
            return jsonify({
                'ranking': _ranking_cache,
                'updated': _fmt_ts(_ranking_cache_time),
                'cached_at': _iso_kst(_ranking_cache_time),
                'note': '캐시된 결과 (서버 절전 모드)',
                'chrome_mode': chrome.mode(),
            })
        # 2순위: 디스크 캐시 (24시간 이내, 컨테이너 재시작 후에도 살아남음)
        disk_data, disk_ts = _load_disk_cache('ranking')
        if disk_data:
            return jsonify({
                'ranking': disk_data,
                'updated': _fmt_ts(disk_ts),
                'cached_at': _iso_kst(disk_ts),
                'note': '디스크 캐시 (서버 절전 모드, 어제 기준)',
                'chrome_mode': chrome.mode(),
            })
        return jsonify(_empty_payload('ranking'))
    ranking = get_team_ranking()
    return jsonify({'ranking': ranking, 'updated': _fmt_ts(_ranking_cache_time)})


@app.route('/api/recent')
def recent_games():
    team = request.args.get('team', '')
    if not team:
        return jsonify({'error': '팀명을 입력해주세요'}), 400
    # Chrome OFF 가드: 팀별 캐시가 있으면 반환, 없으면 디스크 fallback, 그래도 없으면 빈 응답
    if not chrome.is_active():
        # 1순위: 메모리 캐시
        if team in _recent_cache:
            return jsonify({
                'team': team,
                'recent': _recent_cache[team],
                'updated': _fmt_ts(_recent_cache_time.get(team, 0)),
                'cached_at': _iso_kst(_recent_cache_time.get(team, 0)),
                'note': '캐시된 결과 (서버 절전 모드)',
                'chrome_mode': chrome.mode(),
            })
        # 2순위: 디스크 캐시
        disk_data, disk_ts = _load_disk_cache(f'recent_{team}')
        if disk_data:
            return jsonify({
                'team': team,
                'recent': disk_data,
                'updated': _fmt_ts(disk_ts),
                'cached_at': _iso_kst(disk_ts),
                'note': '디스크 캐시 (서버 절전 모드, 어제 기준)',
                'chrome_mode': chrome.mode(),
            })
        payload = _empty_payload('recent')
        payload['team'] = team
        return jsonify(payload)
    recent = get_recent_games(team)
    return jsonify({
        'team': team,
        'recent': recent,
        'updated': _fmt_ts(_recent_cache_time.get(team, 0))
    })


@app.route('/api/debug/scores')
def debug_scores():
    """스코어 디버그용 - 캐시 무시하고 새로 조회"""
    scores = get_live_scores(force=True)
    return jsonify({'scores': scores, 'updated': _fmt_ts(_scores_cache_time)})


@app.route('/api/debug/scheduler')
def debug_scheduler():
    """스케줄러 진단 - 시스템 TZ, 현재 시각, 등록된 jobs 목록을 반환.
    schedule.every().day.at()이 의도한 시각에 발사되는지 확인용."""
    import time as _t
    sys_now = datetime.now()  # naive — 시스템 로컬 시간
    kst_now = datetime.now(KST)
    jobs_info = []
    for j in schedule.jobs:
        jobs_info.append({
            'tags': list(j.tags),
            'next_run': str(j.next_run) if j.next_run else None,
            'at_time': str(j.at_time) if hasattr(j, 'at_time') and j.at_time else None,
            'interval': j.interval,
            'unit': j.unit,
        })
    return jsonify({
        'chrome_mode': chrome.mode(),
        'chrome_active': chrome.is_active(),
        'scheduler_started': _scheduler_started,
        'CHROME_ALWAYS_ON': _CHROME_ALWAYS_ON,
        'system_tzname': list(_t.tzname),
        'system_now_naive': sys_now.strftime('%Y-%m-%d %H:%M:%S'),
        'kst_now': kst_now.strftime('%Y-%m-%d %H:%M:%S'),
        'tz_offset_hours': (sys_now - kst_now.replace(tzinfo=None)).total_seconds() / 3600,
        'jobs_count': len(schedule.jobs),
        'jobs': jobs_info,
    })


@app.route('/api/debug/warm')
def debug_warm():
    """수동 워밍 트리거 — game_mode 잡이 어떤 이유로 발사 안 됐을 때 강제로 한 사이클 돌린다.
    chrome 강제 ON → _warm_caches_once → chrome OFF(절전 복귀).
    호출 후 /api/scores·/api/gameinfo 등이 채워진다."""
    if _CHROME_ALWAYS_ON:
        return jsonify({'note': 'CHROME_ALWAYS_ON 모드 — 워밍 불필요', 'chrome_active': chrome.is_active()})
    err = None
    started = False
    try:
        chrome.start("debug_warm")
        started = True
        _warm_caches_once()
    except Exception as e:
        err = str(e)
    finally:
        if started:
            # chrome.stop은 finally 안에서도 안전하게 — 여기서 예외 나면 Flask 500이 됨
            try:
                chrome.stop("debug_warm_done")
            except Exception as e2:
                print(f"[debug_warm chrome.stop 오류] {e2}")
    if err:
        return jsonify({'error': err, 'note': 'warming 중 오류'}), 500
    return jsonify({
        'note': '수동 워밍 완료 — scores/gameinfo/ranking/recent 디스크·메모리 갱신',
        'scores_cache_date': _scores_cache_date,
        'scores_count': len(_scores_cache) if _scores_cache else 0,
        'chrome_mode': chrome.mode(),
    })


@app.route('/api/debug/raw')
def debug_raw():
    """진단용: KBO 페이지 raw 라인 덤프 (우천취소 등 파싱 디버깅).
    kind=gamecenter (기본): GameCenter 페이지 / kind=ranking: 팀순위 페이지.
    Selenium으로 받은 body 텍스트를 줄 단위로 그대로 반환하여
    실제 텍스트 구조와 파싱 로직 불일치를 진단한다."""
    kind = request.args.get('kind', 'gamecenter')
    today = get_game_date()
    if kind == 'ranking':
        body = _fetch_body_text(
            'https://m.koreabaseball.com/Kbo/TeamRank.aspx',
            wait_patterns=[r'(LG|KT|SSG|NC|두산|KIA|롯데|삼성|한화|키움)']
        )
        lines = [l.strip() for l in (body or '').split('\n') if l.strip()]
        return jsonify({
            'kind': 'ranking', 'today': today,
            'body_is_none': body is None,
            'line_count': len(lines), 'lines': lines,
        })
    # gamecenter (기본)
    url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
    body = _fetch_body_text(url, wait_patterns=[r'경기예정', r'경기종료', r'\d+회[초말]'])
    lines = [l.strip() for l in (body or '').split('\n') if l.strip()]
    return jsonify({
        'kind': 'gamecenter', 'today': today,
        'body_is_none': body is None,
        'line_count': len(lines), 'lines': lines,
    })


# ─────────────────────────────────────────
# 모듈 로드 시점에 프리워밍 스레드 기동
# (gunicorn/waitress 환경 포함. dev의 리로더는 use_reloader=False로 회피)
# ─────────────────────────────────────────
_start_background()
_start_scheduler()  # CHROME_ALWAYS_ON=true면 내부에서 no-op


if __name__ == '__main__':
    # debug=True의 리로더는 모듈을 2번 import해 스케줄러가 중복 기동되므로 비활성화
    app.run(port=5000, debug=True, use_reloader=False)
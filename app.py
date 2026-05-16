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

STADIUMS = ['잠실', '문학', '광주', '고척', '대전', '수원', '사직', '창원', '대구', '인천', '청주']


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

_CHROME_ALWAYS_ON = os.environ.get('CHROME_ALWAYS_ON', 'true').lower() == 'true'

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
        STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주']
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
        STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주']
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

            if '경기예정' in status_line:
                if i + 5 < len(lines):
                    away_raw = lines[i + 3]
                    vs_check = lines[i + 4]
                    home_raw = lines[i + 5]
                    if 'VS' in vs_check.upper():
                        result = {
                            'status': 'pre',
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
        teams_order = []
        stats_list  = []

        for line in lines:
            m = re.match(r'^(\d+)\s+(LG|KT|SSG|NC|두산|KIA|롯데|삼성|한화|키움)$', line)
            if m:
                teams_order.append({'rank': m.group(1), 'team': m.group(2)})
            s = re.match(r'^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([-\d.]+)\s+(.+)$', line)
            if s:
                stats_list.append({
                    'games': s.group(1), 'win':    s.group(2),
                    'lose':  s.group(3), 'draw':   s.group(4),
                    'pct':   s.group(5), 'gb':     s.group(6),
                    'streak':s.group(7)
                })

        ranking = []
        for i, t in enumerate(teams_order):
            stat = stats_list[i] if i < len(stats_list) else {}
            ranking.append({
                'rank': t['rank'], 'team': t['team'],
                'games': stat.get('games',''), 'win':  stat.get('win',''),
                'lose':  stat.get('lose',''),  'draw': stat.get('draw',''),
                'pct':   stat.get('pct',''),   'gb':   stat.get('gb',''),
                'streak':stat.get('streak','')
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

def _warm_caches_once():
    try:
        today = get_game_date()
        get_live_scores(force=True)  # 내부에서 snapshot도 force=True로 처리

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
            get_team_ranking(force=True)
        except Exception as e:
            print(f"[prewarm ranking 오류] {e}")

        for team in KBO_TEAMS:
            try:
                get_recent_games(team, force=True)
            except Exception as e:
                print(f"[prewarm recent {team} 오류] {e}")
    except Exception as e:
        print(f"[prewarm 사이클 오류] {e}")


def _bg_refresh_loop():
    time_module.sleep(2)  # 서버 기동 직후 혼잡 방지
    while True:
        interval = 300  # 기본: 5분
        try:
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
    """일정에서 가장 늦은 경기 시작 시각 + 4시간(평균 경기 길이). 없으면 None."""
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
    return latest + timedelta(hours=4) if latest else None


def morning_schedule_fetch():
    """매일 03:55 트리거. 오늘 일정 받고 게임 모드 예약 후 Chrome OFF.
    휴식일(경기 없음)은 즉시 OFF하고 다음 날 03:55까지 대기."""
    try:
        today = get_game_date()
        chrome.start("morning_schedule_fetch")
        # 일정은 _get_schedule_rows()로 받지만 Selenium 사용 안 함 (일반 HTTP)
        games = get_kbo_schedule(today)
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
    """마지막 경기 +5m 또는 종료 감지 후 트리거. Chrome OFF + 다음 날 준비."""
    chrome.stop("game_mode_end")
    print(f"[scheduler] 게임 모드 종료 - Chrome OFF")
    _reschedule_next_morning()
    return schedule.CancelJob


def _schedule_game_mode(first_game_kst, last_game_kst):
    """game_start/stop 시각을 schedule에 등록 (일회성, 태그 기반)."""
    start_at = (first_game_kst - timedelta(hours=2, minutes=5)).strftime('%H:%M')
    stop_at = (last_game_kst + timedelta(minutes=5)).strftime('%H:%M')
    schedule.clear('game_start_once')
    schedule.clear('game_stop_once')
    schedule.every().day.at(start_at).do(start_game_mode).tag('game_start_once')
    schedule.every().day.at(stop_at).do(stop_game_mode).tag('game_stop_once')


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

    if _is_test_mode():
        # 테스트: 가까운 미래로 트리거 시각 재배치 (시계 자체를 건드리지 않음)
        now = datetime.now(KST)
        t1 = (now + timedelta(minutes=1)).strftime('%H:%M')
        t2 = (now + timedelta(minutes=2)).strftime('%H:%M')
        t3 = (now + timedelta(minutes=5)).strftime('%H:%M')
        schedule.every().day.at(t1).do(morning_schedule_fetch).tag('test')
        schedule.every().day.at(t2).do(start_game_mode).tag('test')
        schedule.every().day.at(t3).do(stop_game_mode).tag('test')
        print(f"[TEST_MODE] schedule_fetch={t1} game_start={t2} game_stop={t3}")
    else:
        schedule.every().day.at("03:55").do(morning_schedule_fetch).tag('morning')
        print(f"[scheduler] 매일 03:55에 morning_schedule_fetch 예약 (시스템 로컬 시간 기준 - Railway는 TZ=Asia/Seoul 필요)")

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
                if info and info.get('status') == 'pre':
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


@app.route('/api/schedule/today')
def today_schedule():
    team      = request.args.get('team')
    today_str = get_game_date()
    today     = datetime.strptime(today_str, '%Y%m%d')
    games     = get_kbo_schedule(today_str)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]

    # ✅ 오늘 경기가 없으면 내일 경기 조회
    if not games:
        tomorrow_str = (today + timedelta(days=1)).strftime('%Y%m%d')
        tomorrow     = today + timedelta(days=1)
        games        = get_kbo_schedule(tomorrow_str)
        if team:
            games = [g for g in games if team in g['away'] or team in g['home']]
        return jsonify({
            '날짜': tomorrow.strftime('%Y-%m-%d'),
            '경기목록': games,
            '경기수': len(games),
            '내일경기': True
        })

    return jsonify({'날짜': today.strftime('%Y-%m-%d'), '경기목록': games, '경기수': len(games)})


@app.route('/api/pitcher/today')
def pitcher_today():
    """선발투수 전용 API — 무거운 Selenium 호출을 schedule API와 분리"""
    team      = request.args.get('team')
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
    scores = get_live_scores()
    if team:
        scores = [s for s in scores if team in s['away'] or team in s['home']]
    return jsonify({'scores': scores, 'updated': _fmt_ts(_scores_cache_time)})


@app.route('/api/gameinfo')
def game_info():
    team  = request.args.get('team', '')
    today = get_game_date()

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
    ranking = get_team_ranking()
    return jsonify({'ranking': ranking, 'updated': _fmt_ts(_ranking_cache_time)})


@app.route('/api/recent')
def recent_games():
    team = request.args.get('team', '')
    if not team:
        return jsonify({'error': '팀명을 입력해주세요'}), 400
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


# ─────────────────────────────────────────
# 모듈 로드 시점에 프리워밍 스레드 기동
# (gunicorn/waitress 환경 포함. dev의 리로더는 use_reloader=False로 회피)
# ─────────────────────────────────────────
_start_background()
_start_scheduler()  # CHROME_ALWAYS_ON=true면 내부에서 no-op


if __name__ == '__main__':
    # debug=True의 리로더는 모듈을 2번 import해 스케줄러가 중복 기동되므로 비활성화
    app.run(port=5000, debug=True, use_reloader=False)
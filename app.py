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

app = Flask(__name__, static_folder='static')
CORS(app)

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
        "spotv": "107", "spotv2": "108", "kbs_n_sports": "133",
        "mbc_sports": "130", "sbs_sports": "131",
        "kbs2": "7", "mbc": "11", "sbs": "13",
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
    global _scores_cache, _scores_cache_time
    now = time_module.time()
    if not force and _scores_cache and now - _scores_cache_time < _TTL_SCORES:
        return _scores_cache

    today = get_game_date()
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
                        away_score = lines[i+3]
                        home_score = lines[vs_idx+1] if vs_idx+1 < len(lines) else ''
                        if away_score.isdigit() and home_score.isdigit() and teams:
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
        _scores_cache = scores
        _scores_cache_time = time_module.time()
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

                if vs_idx:
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

        for m in [month, f'{int(month)-1:02d}']:
            if int(m) < 1:
                continue
            data = {
                'leId': '1', 'srIdList': '0,9',
                'seasonId': year, 'year': year,
                # teamId 제거 - KBO API가 teamId 필터 시 rows:0 반환
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

                    # 양쪽에 KBO 팀명이 없으면 실제 경기 점수가 아님 (시리즈 전적 등 오파싱 방지)
                    if not any(t in team1 for t in KBO_TEAMS):
                        continue
                    if not any(t in m2.group(4) for t in KBO_TEAMS):
                        continue

                    # 동점은 시리즈 전적 오파싱 가능성 높으므로 제외 (KBO 무승부는 극히 드뭄)
                    if score1 == score2:
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
        _get_gamecenter_snapshot(today, force=True)
        get_live_scores(force=True)

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
    filename = LOGO_FILES.get(team)
    if filename:
        png_path = os.path.join('static', 'logos', filename)
        if os.path.exists(png_path):
            return send_from_directory('static/logos', filename)

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
    from flask import make_response
    resp = make_response(send_file(img_data, mimetype='image/png'))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/schedule/today')
def today_schedule():
    team      = request.args.get('team')
    today_str = get_game_date()
    today     = datetime.strptime(today_str, '%Y%m%d')
    games     = get_kbo_schedule(today_str)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]
    return jsonify({'날짜': today.strftime('%Y-%m-%d'), '경기목록': games, '경기수': len(games)})


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
        'game_id': game_id, 'away': away_name, 'home': home_name,
        'status': gc['status'],
        'away_pitchers': gc.get('away_pitchers', []),
        'home_pitchers': gc.get('home_pitchers', []),
        'updated': _fmt_ts(updated_ts)
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


if __name__ == '__main__':
    # debug=True의 리로더는 모듈을 2번 import해 스케줄러가 중복 기동되므로 비활성화
    app.run(port=5000, debug=True, use_reloader=False)
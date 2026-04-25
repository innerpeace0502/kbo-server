from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import requests
import re
import os
import json as json_module
import time as time_module

app = Flask(__name__, static_folder='static')
CORS(app)

KST = timezone(timedelta(hours=9))

_ranking_cache      = []
_ranking_cache_time = 0
_gameinfo_cache      = {}
_gameinfo_cache_time = {}
_scores_cache      = []
_scores_cache_time = 0
_recent_cache      = {}
_recent_cache_time = {}

channel_map = {
    "genie": {
        "spotv": "51", "spotv2": "52", "kbs_n_sports": "59",
        "mbc_sports": "60", "sbs_sports": "58",
        "kbs2": "7", "mbc": "11", "sbs": "5",
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


def _get_schedule_rows(today):
    year = today[:4]
    month = today[4:6]
    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': year, 'year': year,
        'month': month, 'gameMonth': month, 'teamId': ''
    }
    res = requests.post(
        'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
        headers=_get_kbo_headers(), data=data, timeout=10
    )
    return res.json()


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
                    away_text, home_text = parse_teams_from_score(cell)
                    if away_text and home_text:
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
            for cell in row:
                cell_text = re.sub(r'<[^>]+>', '', cell.get('Text', '')).strip()
                for s in STAD_LIST:
                    if s in cell_text:
                        stadium_map[s] = (away, home)
                        break
    except Exception as e:
        print(f"[구장맵 오류] {e}")
    return stadium_map


def _get_selenium_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options

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

    for cd in ['/usr/bin/chromedriver', '/usr/lib/chromium/chromedriver']:
        if os.path.exists(cd):
            from selenium.webdriver.chrome.service import Service as S
            return webdriver.Chrome(service=S(cd), options=options)

    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service as S
    return webdriver.Chrome(service=S(ChromeDriverManager().install()), options=options)


def _get_gamecenter_lines(today):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = _get_selenium_driver()
    try:
        url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time_module.sleep(3)
        body_text = driver.find_element(By.TAG_NAME, 'body').text
        return [l.strip() for l in body_text.split('\n') if l.strip()]
    finally:
        driver.quit()


def get_live_scores():
    global _scores_cache, _scores_cache_time
    now = time_module.time()
    if _scores_cache and now - _scores_cache_time < 120:
        print("[스코어] 캐시 반환")
        return _scores_cache

    today = get_game_date()
    try:
        driver = _get_selenium_driver()
    except Exception as e:
        print(f"[Selenium 초기화 오류] {e}")
        return _scores_cache

    scores = []
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time_module.sleep(3)

        body_text = driver.find_element(By.TAG_NAME, 'body').text
        lines = [l.strip() for l in body_text.split('\n') if l.strip()]
        stadium_team_map = _get_today_stadium_map(today)

        i = 0
        while i < len(lines):
            line = lines[i]
            stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
            if not (stadium_match and re.search(r'\d{2}:\d{2}', line)):
                i += 1
                continue
            try:
                if i + 2 >= len(lines):
                    i += 1
                    continue
                status_line = lines[i + 2]
                teams = stadium_team_map.get(stadium_match)

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
                    for k in range(i+3, min(i+10, len(lines))):
                        if lines[k].upper() == 'VS':
                            vs_idx = k
                            break
                    if vs_idx and vs_idx > i+3:
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
        print(f"[Selenium 오류] {e}")
    finally:
        driver.quit()

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


def get_pitcher_from_gamecenter(today, game_id):
    global _gameinfo_cache, _gameinfo_cache_time
    now = time_module.time()
    cache_key = game_id

    if cache_key in _gameinfo_cache and now - _gameinfo_cache_time.get(cache_key, 0) < 120:
        print(f"[gameinfo] 캐시 반환: {cache_key}")
        return _gameinfo_cache[cache_key]

    try:
        lines = _get_gamecenter_lines(today)
        stadium_team_map = _get_today_stadium_map(today)
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
                            'away_pitcher': re.sub(r'^선', '', away_raw).strip(),
                            'home_pitcher': re.sub(r'^선', '', home_raw).strip(),
                        }
                        _gameinfo_cache[cache_key] = result
                        _gameinfo_cache_time[cache_key] = time_module.time()
                        return result

            elif '경기종료' in status_line:
                # ✅ 경기종료: VS 위치 동적 탐색 후 승/패/세 파싱
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
                        prefix = raw[0]
                        name   = raw[1:].strip()
                        label  = {'승':'승','패':'패','세':'세','홀':'홀'}.get(prefix, prefix)
                        away_pitchers.append({'label': label, 'name': name})
                    for k in range(vs_idx+2, min(vs_idx+6, len(lines))):
                        raw = lines[k]
                        if not raw or raw[0] not in ('승','패','세','홀'):
                            break
                        prefix = raw[0]
                        name   = raw[1:].strip()
                        label  = {'승':'승','패':'패','세':'세','홀':'홀'}.get(prefix, prefix)
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


def get_team_ranking():
    global _ranking_cache, _ranking_cache_time
    now = time_module.time()
    if _ranking_cache and now - _ranking_cache_time < 600:
        print("[순위] 캐시 반환")
        return _ranking_cache

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    try:
        driver = _get_selenium_driver()
        driver.get('https://m.koreabaseball.com/Kbo/TeamRank.aspx')
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time_module.sleep(2)
        body = driver.find_element(By.TAG_NAME, 'body').text
        driver.quit()

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


def get_recent_games(team):
    global _recent_cache, _recent_cache_time
    now = time_module.time()
    if team in _recent_cache and now - _recent_cache_time.get(team, 0) < 600:
        print(f"[최근경기] 캐시 반환: {team}")
        return _recent_cache[team]

    try:
        today = get_game_date()
        year  = today[:4]
        month = today[4:6]
        headers = _get_kbo_headers()
        results = []

        # ✅ teamId 없이 전체 조회 후 팀 이름으로 필터링
        for m in [month, f'{int(month)-1:02d}']:
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
                    cells = [strip_html(c.get('Text', '')) for c in row]
                    cells = [c for c in cells if c]

                    # ✅ 해당 팀이 포함된 경기 셀 찾기 (vs 포함)
                    game_cell = next((c for c in cells
                                     if 'vs' in c.lower() and team in c
                                     and re.search(r'\d', c)), None)
                    if not game_cell:
                        continue

                    # ✅ 점수 파싱으로 승/패/무 판단
                    # 예: 'KIA2vs7LG', 'LG7vs5두산'
                    m2 = re.search(r'(.+?)(\d+)vs(\d+)(.+)', game_cell)
                    if not m2:
                        continue

                    team1  = m2.group(1).strip()
                    score1 = int(m2.group(2))
                    score2 = int(m2.group(3))
                    team2  = m2.group(4).strip()

                    if team in team1:
                        result = '승' if score1 > score2 else ('패' if score1 < score2 else '무')
                    else:
                        result = '승' if score2 > score1 else ('패' if score2 < score1 else '무')
                    results.append(result)

            except Exception as e:
                print(f"[최근경기 월별 오류] {e}")

        # ✅ 최근 10경기 (오래된→최근 순)
        recent = results[-10:] if len(results) >= 10 else results
        print(f"[최근경기] {team}: {recent}")

        _recent_cache[team] = recent
        _recent_cache_time[team] = time_module.time()
        return recent

    except Exception as e:
        print(f"[최근경기 오류] {e}")
        return []


# ─────────────────────────────────────────
# Flask 라우트
# ─────────────────────────────────────────

@app.route('/')
def home():
    return jsonify({'상태': '서버 정상 작동중!', '시간': datetime.now(KST).strftime('%Y-%m-%d %H:%M')})


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
    return send_file(img_data, mimetype='image/png')


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
    return jsonify({'scores': scores, 'updated': datetime.now(KST).strftime('%H:%M:%S')})


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
    if not gc:
        return jsonify({
            'game_id': game_id, 'away': away_name, 'home': home_name,
            'status': 'unknown',
            'away_pitchers': [], 'home_pitchers': [],
            'updated': datetime.now(KST).strftime('%H:%M:%S')
        })

    return jsonify({
        'game_id': game_id, 'away': away_name, 'home': home_name,
        'status': gc['status'],
        'away_pitchers': gc.get('away_pitchers', []),
        'home_pitchers': gc.get('home_pitchers', []),
        'updated': datetime.now(KST).strftime('%H:%M:%S')
    })


@app.route('/api/ranking')
def team_ranking():
    ranking = get_team_ranking()
    return jsonify({'ranking': ranking, 'updated': datetime.now(KST).strftime('%H:%M:%S')})


@app.route('/api/recent')
def recent_games():
    team = request.args.get('team', '')
    if not team:
        return jsonify({'error': '팀명을 입력해주세요'}), 400
    recent = get_recent_games(team)
    return jsonify({'team': team, 'recent': recent, 'updated': datetime.now(KST).strftime('%H:%M:%S')})


if __name__ == '__main__':
    app.run(port=5000, debug=True)
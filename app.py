from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from datetime import datetime, timezone, timedelta
import requests
import re
import os
import json as json_module

app = Flask(__name__, static_folder='static')
CORS(app)

KST = timezone(timedelta(hours=9))

channel_map = {
    "genie": {
        "spotv": "51", "spotv2": "52", "kbs_n_sports": "133",
        "mbc_sports": "130", "sbs_sports": "131",
        "kbs2": "7", "mbc": "11", "sbs": "13",
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
    "SPO-T":  "spotv",
    "SPO-2T": "spotv2",
    "KN-T":   "kbs_n_sports",
    "MBC-SP": "mbc_sports",
    "MS-T":   "mbc_sports",
    "SS-T":   "sbs_sports",
    "S-T":    "sbs",
    "M-T":    "mbc",
    "K-2T":   "kbs2",
    "TVING":  "tving",
}

LOGO_FILES = {
    "LG":  "lg.png",  "KT":  "kt.png",  "SSG": "ssg.png",
    "NC":  "nc.png",  "두산": "doosan.png", "KIA": "kia.png",
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
    base = "https://web-production-6aae76.up.railway.app"
    return f"{base}/logos/{team}"


def get_game_date():
    now = datetime.now(KST)
    if now.hour < 4:
        game_date = now - timedelta(days=1)
    else:
        game_date = now
    return game_date.strftime('%Y%m%d')


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


def _get_kbo_schedule_headers():
    return {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.koreabaseball.com/',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
    }


def _get_schedule_rows(today):
    year = today[:4]
    month = today[4:6]
    headers = _get_kbo_schedule_headers()
    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': year, 'year': year,
        'month': month, 'gameMonth': month, 'teamId': ''
    }
    res = requests.post(
        'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
        headers=headers, data=data, timeout=10
    )
    return res.json()


def get_kbo_schedule(date_str):
    year  = date_str[:4]
    month = date_str[4:6]
    day   = date_str[6:8]
    target_date = f"{month}.{day}"
    try:
        result = _get_schedule_rows(date_str)
        games = []
        current_date = ""
        for row_obj in result.get('rows', []):
            row = row_obj.get('row', [])
            if not row:
                continue
            cells = [strip_html(cell.get('Text', '')) for cell in row]
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
            STAD_LIST = ['잠실','수원','창원','대구','광주','인천','문학','대전','사직','고척','청주']
            stadium_text = next((c for c in cells if any(s in c for s in STAD_LIST)), '')
            if away_text and home_text:
                games.append({
                    'time':      time_text,
                    'away':      away_text,
                    'home':      home_text,
                    'stadium':   stadium_text,
                    'broadcast': broadcast,
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
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    chromium_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']
    for path in chromium_paths:
        if os.path.exists(path):
            options.binary_location = path
            break

    chromedriver_paths = ['/usr/bin/chromedriver', '/usr/lib/chromium/chromedriver']
    for cd_path in chromedriver_paths:
        if os.path.exists(cd_path):
            from selenium.webdriver.chrome.service import Service as ChromeService
            return webdriver.Chrome(service=ChromeService(cd_path), options=options)

    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.service import Service as ChromeService
    return webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)


def _get_gamecenter_lines(today):
    """게임센터 페이지 텍스트 라인 반환 (공통)"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time

    driver = _get_selenium_driver()
    try:
        url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(3)
        body_text = driver.find_element(By.TAG_NAME, 'body').text
        return [l.strip() for l in body_text.split('\n') if l.strip()]
    finally:
        driver.quit()


def get_live_scores():
    """Selenium으로 KBO 게임센터에서 실시간 스코어 크롤링"""
    today = get_game_date()

    try:
        driver = _get_selenium_driver()
    except Exception as e:
        print(f"[Selenium 초기화 오류] {e}")
        return []

    scores = []
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import time

        url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
        driver.get(url)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(3)

        body_text = driver.find_element(By.TAG_NAME, 'body').text
        lines = [l.strip() for l in body_text.split('\n') if l.strip()]
        stadium_team_map = _get_today_stadium_map(today)

        i = 0
        while i < len(lines):
            line = lines[i]
            stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
            if stadium_match and re.search(r'\d{2}:\d{2}', line):
                try:
                    if i + 2 >= len(lines):
                        i += 1
                        continue

                    status_line = lines[i + 2]
                    teams = stadium_team_map.get(stadium_match)

                    if '경기예정' in status_line:
                        # 경기 전: status=0, 점수없음
                        if teams:
                            scores.append({
                                'away': teams[0], 'home': teams[1],
                                'away_score': '', 'home_score': '',
                                'status': '0', 'inning': ''
                            })
                        i += 6
                        continue

                    elif '경기종료' in status_line:
                        # ✅ 경기 종료: [경기종료] [원정점수] [VS] [홈점수]
                        if i + 4 < len(lines):
                            away_score = lines[i + 3]
                            vs_line    = lines[i + 4] if i + 4 < len(lines) else ''
                            home_score = lines[i + 5] if i + 5 < len(lines) else ''

                            # 종료 시 VS 바로 다음이 홈점수
                            if 'VS' in (lines[i + 3] if i + 3 < len(lines) else '').upper():
                                # [경기종료] [VS] [홈점수] 구조인 경우
                                away_score = lines[i + 1] if i + 1 < len(lines) else ''
                                vs_line    = lines[i + 2] if i + 2 < len(lines) else ''
                                home_score = lines[i + 3] if i + 3 < len(lines) else ''
                            elif 'VS' in (lines[i + 4] if i + 4 < len(lines) else '').upper():
                                # ✅ [경기종료] [원정점수] ... [VS] ... 구조
                                away_score = lines[i + 3]
                                vs_line    = lines[i + 4]
                                home_score = lines[i + 5] if i + 5 < len(lines) else ''

                            if away_score.isdigit() and home_score.isdigit():
                                if teams:
                                    scores.append({
                                        'away': teams[0], 'home': teams[1],
                                        'away_score': away_score,
                                        'home_score': home_score,
                                        'status': '2', 'inning': '경기종료'
                                    })
                        i += 8
                        continue

                    elif re.match(r'\d+회[초말]', status_line):
                          # ✅ 경기 중: [이닝] [원정점수] [원정투수] [VS] [홈점수] [홈투수]
                        if i + 7 < len(lines):
                            away_raw = lines[i + 4]   # 원정 투수
                            vs_check = lines[i + 5]   # VS
                            home_raw = lines[i + 7]   # ✅ 홈 투수 (i+6은 홈 점수!)
                            if 'VS' in vs_check.upper():
                                away_p = re.sub(r'^(승|패|홀드|세)', '', away_raw).strip()
                                home_p = re.sub(r'^(승|패|홀드|세)', '', home_raw).strip()
                                return {
                                    'status': 'live',
                                    'away_pitcher': away_p,
                                    'home_pitcher': home_p,
                                    'lineups': {'away': [], 'home': []}
                                }
                                if teams:
                                    scores.append({
                                        'away': teams[0], 'home': teams[1],
                                        'away_score': away_score,
                                        'home_score': home_score,
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


def get_pitcher_and_lineup_from_gamecenter(today, game_id):
    """
    게임센터 Selenium에서 투수+라인업 파싱
    - 경기 예정: 선발투수만
    - 경기 중: 현재 투수
    - 경기 종료: 승리/패전 투수 (GetBoxScoreScroll에서)
    """
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
            print(f"[디버그] i={i} stadium={stadium_match} teams={teams} target_away={target_away}")  # ✅ 추가
            
            if not teams or teams[0] != target_away:
                i += 1
                continue

            if i + 2 >= len(lines):
                break

            status_line = lines[i + 2]
            print(f"[디버그] status_line={status_line}")  # ✅ 추가
            print(f"[디버그] i+3={lines[i+3] if i+3<len(lines) else 'X'}")
            print(f"[디버그] i+4={lines[i+4] if i+4<len(lines) else 'X'}")
            print(f"[디버그] i+5={lines[i+5] if i+5<len(lines) else 'X'}")
            print(f"[디버그] i+6={lines[i+6] if i+6<len(lines) else 'X'}")
            print(f"[디버그] i+7={lines[i+7] if i+7<len(lines) else 'X'}")

            if '경기예정' in status_line:
                # [구장] [채널] [경기예정] [선원정투수] [VS] [선홈투수]
                if i + 5 < len(lines):
                    away_raw = lines[i + 3]
                    vs_check = lines[i + 4]
                    home_raw = lines[i + 5]
                    if 'VS' in vs_check.upper():
                        away_p = re.sub(r'^선', '', away_raw).strip()
                        home_p = re.sub(r'^선', '', home_raw).strip()
                        return {
                            'status': 'pre',
                            'away_pitcher': away_p,
                            'home_pitcher': home_p,
                            'lineups': {'away': [], 'home': []}
                        }

            elif '경기종료' in status_line:
                # ✅ 경기종료: [경기종료] [원정점수] [VS] [홈점수]
                # 투수 정보는 GetBoxScoreScroll에서 가져옴
                return {
                    'status': 'ended',
                    'away_pitcher': '',
                    'home_pitcher': '',
                    'lineups': {'away': [], 'home': []}
                }

            elif re.match(r'\d+회[초말]', status_line):
                if i + 7 < len(lines):
                    away_raw = lines[i + 4]
                    vs_check = lines[i + 5]
                    home_raw = lines[i + 7]

                    if 'VS' in vs_check.upper():
                        is_top = '초' in status_line  # 회초 = 원정팀 공격

                        away_raw_clean = re.sub(r'^(승|패|홀드|세)', '', away_raw).strip()
                        home_raw_clean = re.sub(r'^(승|패|홀드|세)', '', home_raw).strip()

                        if is_top:
                            # 회초: 원정팀 공격(타자), 홈팀 수비(투수)
                            away_p = f'타 {away_raw_clean}'
                            home_p = f'투 {home_raw_clean}'
                        else:
                            # 회말: 원정팀 수비(투수), 홈팀 공격(타자)
                            away_p = f'투 {away_raw_clean}'
                            home_p = f'타 {home_raw_clean}'

                        return {
                            'status': 'live',
                            'away_pitcher': away_p,
                            'home_pitcher': home_p,
                            'lineups': {'away': [], 'home': []}
                        }
            i += 1

    except Exception as e:
        print(f"[게임센터 파싱 오류] {e}")

    return None


def get_box_score(game_id, today):
    """박스스코어 API - 경기 종료 후 투수/라인업"""
    year = today[:4]
    headers = _get_kbo_schedule_headers()
    data = {'gameId': game_id, 'leId': '1', 'srId': '0', 'seasonId': year}
    try:
        res = requests.post(
            'https://www.koreabaseball.com/ws/Schedule.asmx/GetBoxScoreScroll',
            headers=headers, data=data, timeout=10
        )
        result = res.json()

        if result.get('code') != '100':
            return None

        pitchers = []
        for team in result.get('arrPitcher', []):
            table = json_module.loads(team['table'])
            team_pitchers = []
            for row in table['rows']:
                cells = row['row']
                name        = cells[0]['Text']
                timing      = cells[1]['Text']
                result_text = cells[2]['Text'].replace('&nbsp;', '')
                team_pitchers.append({
                    'name': name, 'timing': timing, 'result': result_text
                })
            pitchers.append(team_pitchers)

        lineups = []
        for team in result.get('arrHitter', []):
            table = json_module.loads(team['table1'])
            seen = set()
            team_lineup = []
            for row in table['rows']:
                cells = row['row']
                order = cells[0]['Text']
                pos   = cells[1]['Text']
                name  = cells[2]['Text']
                if order not in seen:
                    seen.add(order)
                    team_lineup.append({'order': order, 'pos': pos, 'name': name})
            lineups.append(team_lineup)

        return {'pitchers': pitchers, 'lineups': lineups}
    except Exception as e:
        print(f"[박스스코어 오류] {e}")
        return None


def get_team_ranking():
    """KBO 모바일에서 팀 순위 크롤링"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time

    try:
        driver = _get_selenium_driver()
        driver.get('https://m.koreabaseball.com/Kbo/TeamRank.aspx')
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(2)
        body  = driver.find_element(By.TAG_NAME, 'body').text
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
                    'games':  s.group(1), 'win':    s.group(2),
                    'lose':   s.group(3), 'draw':   s.group(4),
                    'pct':    s.group(5), 'gb':     s.group(6),
                    'streak': s.group(7)
                })

        ranking = []
        for i, t in enumerate(teams_order):
            stat = stats_list[i] if i < len(stats_list) else {}
            ranking.append({
                'rank':   t['rank'], 'team':  t['team'],
                'games':  stat.get('games', ''), 'win':  stat.get('win', ''),
                'lose':   stat.get('lose', ''),  'draw': stat.get('draw', ''),
                'pct':    stat.get('pct', ''),   'gb':   stat.get('gb', ''),
                'streak': stat.get('streak', '')
            })
        return ranking

    except Exception as e:
        print(f"[순위 오류] {e}")
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
        "LG":  (195, 4, 82),  "KT":  (227, 30, 38), "SSG": (206, 14, 45),
        "NC":  (7, 29, 73),   "두산": (19, 18, 48),  "KIA": (234, 0, 41),
        "롯데": (4, 30, 66),   "삼성": (0, 85, 168),  "한화": (255, 102, 0),
        "키움": (130, 0, 36)
    }
    color = team_colors.get(team, (68, 68, 68))
    size  = 120
    img   = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=color)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except Exception:
        font = ImageFont.load_default()
    text = team[:2]
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2), text, fill=(255, 255, 255), font=font)
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
    """투수 + 라인업 통합 API"""
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

    # 1. 게임센터에서 투수 파싱
    gc = get_pitcher_and_lineup_from_gamecenter(today, game_id)

    if gc and gc['status'] == 'ended':
        # 경기 종료 → 박스스코어에서 투수/라인업
        box = get_box_score(game_id, today)
        if box and len(box.get('pitchers', [])) > 0:
            return jsonify({
                'game_id': game_id, 'away': away_name, 'home': home_name,
                'status': 'ended',
                'pitchers': {
                    'away': box['pitchers'][0] if len(box['pitchers']) > 0 else [],
                    'home': box['pitchers'][1] if len(box['pitchers']) > 1 else []
                },
                'lineups': {
                    'away': box['lineups'][0] if len(box['lineups']) > 0 else [],
                    'home': box['lineups'][1] if len(box['lineups']) > 1 else []
                },
                'updated': datetime.now(KST).strftime('%H:%M:%S')
            })

    if gc and gc['status'] in ('pre', 'live'):
        status_label = gc['status']
        away_p = gc['away_pitcher']
        home_p = gc['home_pitcher']
        return jsonify({
            'game_id': game_id, 'away': away_name, 'home': home_name,
            'status': status_label,
            'pitchers': {
                'away': [{'name': away_p, 'timing': '선발' if status_label == 'pre' else '현재', 'result': ''}] if away_p else [],
                'home': [{'name': home_p, 'timing': '선발' if status_label == 'pre' else '현재', 'result': ''}] if home_p else []
            },
            'lineups': {'away': [], 'home': []},
            'updated': datetime.now(KST).strftime('%H:%M:%S')
        })

    return jsonify({
        'game_id': game_id, 'away': away_name, 'home': home_name,
        'status': 'unknown',
        'pitchers': {'away': [], 'home': []},
        'lineups':  {'away': [], 'home': []},
        'updated':  datetime.now(KST).strftime('%H:%M:%S')
    })


@app.route('/api/ranking')
def team_ranking():
    ranking = get_team_ranking()
    return jsonify({'ranking': ranking, 'updated': datetime.now(KST).strftime('%H:%M:%S')})


@app.route('/api/debug/chrome')
def debug_chrome():
    import subprocess
    paths_to_check = [
        '/nix/var/nix/profiles/default/bin/chromium',
        '/nix/var/nix/profiles/default/bin/chromedriver',
        '/usr/bin/chromium', '/usr/bin/chromedriver', '/usr/bin/chromium-browser',
    ]
    result = {}
    for path in paths_to_check:
        result[path] = os.path.exists(path)
    for cmd in ['chromium', 'chromedriver', 'google-chrome', 'chromium-browser']:
        try:
            out = subprocess.check_output(['which', cmd], stderr=subprocess.DEVNULL).decode().strip()
            result[f'which_{cmd}'] = out
        except Exception:
            result[f'which_{cmd}'] = 'not found'
    return jsonify(result)


@app.route('/api/debug/raw')
def debug_raw():
    try:
        today  = datetime.now(KST).strftime('%Y%m%d')
        result = _get_schedule_rows(today)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)})


if __name__ == '__main__':
    app.run(port=5000, debug=True)
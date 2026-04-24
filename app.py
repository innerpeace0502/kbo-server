from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
from datetime import datetime
import requests
import re
import os

app = Flask(__name__, static_folder='static')
CORS(app)

channel_map = {
    "olleh": {"spotv": "167", "spotv2": "168", "mbc_sports": "130", "kbs_n_sports": "133"},
    "genie": {"spotv": "51", "spotv2": "52", "mbc_sports": "60", "kbs_n_sports": "63"},
    "btv":   {"spotv": "241", "spotv2": "242", "mbc_sports": "228", "kbs_n_sports": "220"}
}

KBO_TEAMS = ["LG", "KT", "SSG", "NC", "두산", "KIA", "롯데", "삼성", "한화", "키움"]

BROADCAST_MAP = {
    "SPO-T":  "spotv",
    "SPO-2T": "spotv2",
    "MBC-SP": "mbc_sports",
    "KN-T":   "kbs_n_sports",
    "TVING":  "tving"
}

TEAM_LOGOS_SVG = {
    "LG": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#C30452"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="32" fill="white" text-anchor="middle">LG</text>
    </svg>''',
    "KT": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#E31E26"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="32" fill="white" text-anchor="middle">KT</text>
    </svg>''',
    "SSG": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#CE0E2D"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">SSG</text>
    </svg>''',
    "NC": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#071D49"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="32" fill="white" text-anchor="middle">NC</text>
    </svg>''',
    "두산": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#131230"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">두산</text>
    </svg>''',
    "KIA": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#EA0029"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">KIA</text>
    </svg>''',
    "롯데": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#041E42"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">롯데</text>
    </svg>''',
    "삼성": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#0055A8"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">삼성</text>
    </svg>''',
    "한화": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#FF6600"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">한화</text>
    </svg>''',
    "키움": '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="50" fill="#820024"/>
        <text x="50" y="62" font-family="Arial" font-weight="bold" font-size="28" fill="white" text-anchor="middle">키움</text>
    </svg>'''
}

LOGO_FILES = {
    "LG":  "lg.png",
    "KT":  "kt.png",
    "SSG": "ssg.png",
    "NC":  "nc.png",
    "두산": "doosan.png",
    "KIA": "kia.png",
    "롯데": "lotte.png",
    "삼성": "samsung.png",
    "한화": "hanwha.png",
    "키움": "kiwoom.png"
}


def get_logo_url(team):
    base = "https://web-production-6aae76.up.railway.app"
    return f"{base}/logos/{team}"


def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()


def parse_teams_from_score(text):
    clean = re.sub(r'\d+', '', text)
    parts = re.split(r'vs', clean, flags=re.IGNORECASE)
    if len(parts) == 2:
        away = parts[0].strip()
        home = parts[1].strip()
        away_team = next((t for t in KBO_TEAMS if t in away), away)
        home_team = next((t for t in KBO_TEAMS if t in home), home)
        if away_team and home_team:
            return away_team, home_team
    return None, None


def get_kbo_schedule(date_str):
    year  = date_str[:4]
    month = date_str[4:6]
    day   = date_str[6:8]
    target_date = f"{month}.{day}"

    url = "https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.koreabaseball.com/Schedule/Schedule.aspx',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'leId': '1', 'srIdList': '0,9',
        'seasonId': year, 'year': year,
        'month': month, 'gameMonth': month, 'teamId': ''
    }

    try:
        res = requests.post(url, headers=headers, data=data, timeout=10)
        res.encoding = 'utf-8'
        result = res.json()
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

            time_text = next(
                (c for c in cells if re.match(r'\d{2}:\d{2}$', c)), '시간미정'
            )

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

            stadiums = ['잠실', '수원', '창원', '대구', '광주', '인천', '대전', '사직', '고척', '청주']
            stadium_text = next((c for c in cells if any(s in c for s in stadiums)), '')

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
        print(f"[오류] {e}")
        return []


def _get_today_stadium_map(today):
    """KBO API에서 오늘 경기 구장-팀 매핑 가져오기"""
    stadium_map = {}
    try:
        year = today[:4]
        month = today[4:6]
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.koreabaseball.com/',
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = {
            'leId': '1', 'srIdList': '0,9',
            'seasonId': year, 'year': year,
            'month': month, 'gameMonth': month, 'teamId': ''
        }
        res = requests.post(
            'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList',
            headers=headers, data=data, timeout=10
        )
        result = res.json()
        target_date = f"{month}.{today[6:8]}"
        current_date = ''
        STADIUMS = ['잠실', '수원', '창원', '대구', '광주', '인천', '대전', '사직', '고척', '청주']

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
                for s in STADIUMS:
                    if s in cell_text:
                        stadium_map[s] = (away, home)
                        break

    except Exception as e:
        print(f"[구장맵 오류] {e}")

    return stadium_map


def get_live_scores():
    """Selenium으로 KBO 게임센터에서 실시간 스코어 크롤링"""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time

    today = datetime.now().strftime('%Y%m%d')
    STADIUMS = ['잠실', '문학', '광주', '고척', '대전', '수원', '사직', '창원', '대구', '인천', '청주']

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

    # Railway(Linux) 환경 Chromium 경로 지정
    if os.path.exists('/usr/bin/chromium'):
        options.binary_location = '/usr/bin/chromium'
    elif os.path.exists('/usr/bin/chromium-browser'):
        options.binary_location = '/usr/bin/chromium-browser'

    try:
        if os.path.exists('/usr/bin/chromedriver'):
            # Railway 환경
            driver = webdriver.Chrome(
                service=Service('/usr/bin/chromedriver'),
                options=options
            )
        else:
            # 로컬 환경
            from webdriver_manager.chrome import ChromeDriverManager
            driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=options
            )
    except Exception as e:
        print(f"[Selenium 초기화 오류] {e}")
        return []

    scores = []
    try:
        url = f'https://www.koreabaseball.com/Schedule/GameCenter/Main.aspx?gameDate={today}'
        driver.get(url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, 'body'))
        )
        time.sleep(3)

        body_text = driver.find_element(By.TAG_NAME, 'body').text
        lines = [l.strip() for l in body_text.split('\n') if l.strip()]

        # 오늘 경기 구장-팀 매핑
        stadium_team_map = _get_today_stadium_map(today)

        # 패턴 파싱:
        # [구장+시간] [채널] [이닝] [원정점수] [원정투수] [VS] [홈점수] [홈투수]
        i = 0
        while i < len(lines):
            line = lines[i]

            # 구장+시간 패턴 감지 (예: "잠실18:30")
            stadium_match = next((s for s in STADIUMS if line.startswith(s)), None)
            if stadium_match and re.search(r'\d{2}:\d{2}', line):
                try:
                    if i + 7 < len(lines):
                        inning_str = lines[i + 2]
                        away_score = lines[i + 3]
                        vs_line    = lines[i + 5]
                        home_score = lines[i + 6]

                        # 유효성 검사
                        if not re.match(r'\d+회[초말]|종료|경기전', inning_str):
                            i += 1
                            continue
                        if not away_score.isdigit() or not home_score.isdigit():
                            i += 1
                            continue
                        if 'VS' not in vs_line.upper():
                            i += 1
                            continue

                        teams = stadium_team_map.get(stadium_match)
                        if teams:
                            away, home = teams
                            status = '2' if '종료' in inning_str else '1'
                            scores.append({
                                'away':       away,
                                'home':       home,
                                'away_score': away_score,
                                'home_score': home_score,
                                'status':     status,
                                'inning':     inning_str
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


# ─────────────────────────────────────────
# Flask 라우트
# ─────────────────────────────────────────

@app.route('/')
def home():
    return jsonify({'상태': '서버 정상 작동중!', '시간': datetime.now().strftime('%Y-%m-%d %H:%M')})


@app.route('/logos/<team>')
def get_logo(team):
    filename = LOGO_FILES.get(team)
    if filename:
        png_path = os.path.join('static', 'logos', filename)
        if os.path.exists(png_path):
            return send_from_directory('static/logos', filename)

    # PNG 없으면 Pillow로 팀 컬러 원형 PNG 생성
    from PIL import Image, ImageDraw, ImageFont
    import io

    team_colors = {
        "LG":  (195, 4, 82),
        "KT":  (227, 30, 38),
        "SSG": (206, 14, 45),
        "NC":  (7, 29, 73),
        "두산": (19, 18, 48),
        "KIA": (234, 0, 41),
        "롯데": (4, 30, 66),
        "삼성": (0, 85, 168),
        "한화": (255, 102, 0),
        "키움": (130, 0, 36)
    }

    color = team_colors.get(team, (68, 68, 68))
    size = 120

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=color)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
    except:
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
    today     = datetime.today()
    today_str = today.strftime('%Y%m%d')
    games     = get_kbo_schedule(today_str)
    if team:
        games = [g for g in games if team in g['away'] or team in g['home']]
    return jsonify({'날짜': today.strftime('%Y-%m-%d'), '경기목록': games, '경기수': len(games)})


@app.route('/api/schedule/<date>')
def schedule_by_date(date):
    team = request.args.get('team')
    try:
        d = datetime.strptime(date, '%Y%m%d')
    except:
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
    """실시간 스코어 API"""
    team = request.args.get('team')
    scores = get_live_scores()
    if team:
        scores = [s for s in scores if team in s['away'] or team in s['home']]
    return jsonify({'scores': scores, 'updated': datetime.now().strftime('%H:%M:%S')})


@app.route('/api/debug/raw')
def debug_raw():
    try:
        today = datetime.now().strftime('%Y%m%d')
        year  = today[:4]
        month = today[4:6]
        url = "https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList"
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.koreabaseball.com/Schedule/Schedule.aspx',
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = {
            'leId': '1', 'srIdList': '0,9',
            'seasonId': year, 'year': year,
            'month': month, 'gameMonth': month, 'teamId': ''
        }
        res = requests.post(url, headers=headers, data=data, timeout=10)
        return jsonify(res.json())
    except Exception as e:
        return jsonify({'error': str(e)})


if __name__ == '__main__':
    app.run(port=5000, debug=True)
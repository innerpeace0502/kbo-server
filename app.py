from flask import Flask, jsonify, request, send_from_directory
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

# 구단 로고 SVG (서버에서 직접 제공)
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

@app.route('/')
def home():
    return jsonify({'상태': '서버 정상 작동중!', '시간': datetime.now().strftime('%Y-%m-%d %H:%M')})

@app.route('/logos/<team>')
def get_logo(team):
    svg = TEAM_LOGOS_SVG.get(team, TEAM_LOGOS_SVG.get("LG"))
    from flask import Response
    return Response(svg, mimetype='image/svg+xml')

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

if __name__ == '__main__':
    app.run(port=5000, debug=True)
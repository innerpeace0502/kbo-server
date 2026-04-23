from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
import requests
import re

app = Flask(__name__)
CORS(app)

channel_map = {
    "olleh": {"spotv": "167", "spotv2": "168", "mbc_sports": "130", "kbs_n_sports": "133"},
    "genie": {"spotv": "51", "spotv2": "52", "mbc_sports": "60", "kbs_n_sports": "63"},
    "btv":   {"spotv": "241", "spotv2": "242", "mbc_sports": "228", "kbs_n_sports": "220"}
}

KBO_TEAMS = ["LG", "KT", "SSG", "NC", "두산", "KIA", "롯데", "삼성", "한화", "키움"]

# 중계 채널 코드 매핑
BROADCAST_MAP = {
    "SPO-T":  "spotv",
    "SPO-2T": "spotv2",
    "MBC-SP": "mbc_sports",
    "KN-T":   "kbs_n_sports",
    "TVING":  "tving"
}

def strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()

def parse_teams_from_score(text):
    """'KIA2vs7LG' 또는 'KT vs 한화' 형태에서 팀명 추출"""
    # 숫자 제거 후 vs로 분리
    clean = re.sub(r'\d+', '', text)
    parts = re.split(r'vs', clean, flags=re.IGNORECASE)
    if len(parts) == 2:
        away = parts[0].strip()
        home = parts[1].strip()
        # 팀명 목록과 대조
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

            # 날짜 업데이트 (예: '04.23(목)')
            for cell in cells:
                if re.match(r'\d{2}\.\d{2}', cell):
                    current_date = cell[:5]
                    break

            if target_date not in current_date:
                continue

            # 시간 (예: '18:30')
            time_text = next(
                (c for c in cells if re.match(r'\d{2}:\d{2}$', c)), '시간미정'
            )

            # 팀명 파싱 (예: 'KIA2vs7LG' → KIA, LG)
            away_text = home_text = ''
            for cell in cells:
                if 'vs' in cell.lower():
                    away_text, home_text = parse_teams_from_score(cell)
                    if away_text and home_text:
                        break

            # 중계 채널
            broadcast = ''
            for cell in cells:
                for code in BROADCAST_MAP:
                    if code in cell:
                        broadcast = BROADCAST_MAP[code]
                        break
                if broadcast:
                    break

            # 구장
            stadiums = ['잠실', '수원', '창원', '대구', '광주', '인천', '대전', '사직', '고척', '청주']
            stadium_text = next((c for c in cells if any(s in c for s in stadiums)), '')

            if away_text and home_text:
                games.append({
                    'time':      time_text,
                    'away':      away_text,
                    'home':      home_text,
                    'stadium':   stadium_text,
                    'broadcast': broadcast
                })

        return games

    except Exception as e:
        print(f"[오류] {e}")
        return []

@app.route('/')
def home():
    return jsonify({'상태': '서버 정상 작동중!', '시간': datetime.now().strftime('%Y-%m-%d %H:%M')})

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
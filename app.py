from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
import requests
import re

app = Flask(__name__)
CORS(app)

channel_map = {
    "genie": {"spotv": "51", "spotv2": "52", "mbc_sports": "60", "kbs_n_sports": "59"},
    "Uplus": {"spotv": "254", "spotv2": "255", "mbc_sports": "227", "kbs_n_sports": "216"},
    "btv":   {"spotv": "241", "spotv2": "242", "mbc_sports": "228", "kbs_n_sports": "220"}
}

KBO_TEAMS = ["LG", "KT", "SSG", "NC", "두산", "KIA", "롯데", "삼성", "한화", "키움"]

def strip_html(text):
    """HTML 태그 제거"""
    return re.sub(r'<[^>]+>', '', text).strip()

def get_kbo_schedule(date_str):
    year  = date_str[:4]
    month = date_str[4:6]
    day   = date_str[6:8]
    target_date = f"{month}.{day}"  # 예: 04.23

    url = "https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://www.koreabaseball.com/Schedule/Schedule.aspx',
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'leId': '1',
        'srIdList': '0,9',
        'seasonId': year,
        'year': year,
        'month': month,
        'gameMonth': month,
        'teamId': ''
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

            # 각 셀의 텍스트 추출
            cells = [strip_html(cell.get('Text', '')) for cell in row]

            # 날짜 셀 찾기 (예: 04.23(목))
            for cell in cells:
                if re.match(r'\d{2}\.\d{2}', cell):
                    current_date = cell[:5]  # 04.23
                    break

            # 오늘 날짜 필터링
            if target_date not in current_date:
                continue

           # 시간, 팀명 찾기
time_text = away_text = home_text = stadium_text = ''
team_count = 0

for cell in col_texts:
    # 시간 찾기
    clean = re.sub(r'<[^>]+>', '', cell).strip()
    if re.match(r'\d{2}:\d{2}', clean) and not time_text:
        time_text = clean

    # 팀명 찾기 (HTML 제거 후)
    for team in KBO_TEAMS:
        if team in clean and len(clean) <= 5:
            if team_count == 0:
                away_text = team
                team_count += 1
            elif team_count == 1 and team != away_text:
                home_text = team
                team_count += 1
            break

    # 구장 찾기
    stadiums = ['잠실', '수원', '창원', '대구', '광주', '인천', '대전', '사직', '고척', '청주']
    for s in stadiums:
        if s in clean:
            stadium_text = clean
            break

            if len(found_teams) >= 2:
                games.append({
                    'time':    time_text or '시간미정',
                    'away':    found_teams[0],
                    'home':    found_teams[1],
                    'stadium': stadium_text
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
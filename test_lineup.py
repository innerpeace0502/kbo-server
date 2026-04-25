from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
try:
    # 모바일 팀 순위 페이지
    url = 'https://m.koreabaseball.com/Kbo/TeamRank.aspx'
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
    time.sleep(2)

    body = driver.find_element(By.TAG_NAME, 'body').text
    lines = [l.strip() for l in body.split('\n') if l.strip()]
    for i, line in enumerate(lines[:50]):
        print(f'{i:3d}: {line}')
finally:
    driver.quit()
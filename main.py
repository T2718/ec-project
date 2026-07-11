import os
import re
import ast
import pathlib
import datetime
from urllib.parse import urlparse
from flask import Flask, request, render_template_string, Response, stream_with_context
import requests
from bs4 import BeautifulSoup

# Selenium関連のインポート
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

app = Flask(__name__)

# --- サイト設定 ---
siteList = {
    'zozovideo.com': {'name': 'zozo', 'code': 0},
    'jp.spankbang.com': {'name': 'spank', 'code': 1}
}

def getDate():
    return datetime.datetime.now().strftime('%Y/%m/%d-%H:%M')

# --- ヘッドレスChromeの初期化関数 ---
def get_headless_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless')  # 画面非表示
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    # Render環境（Linux）で安定してパスを通す設定
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def getBySelenium(url):
    driver = get_headless_driver()
    try:
        driver.get(url)
        html = driver.page_source
    finally:
        driver.quit()  # メモリリーク防止のため必ず閉じる
    return html

# --- 元のスクレイピングロジック群 ---
def getZozo(soup):
    result = {'title': 'Unknown', 'status': [], 'information': {}}
    video = soup.find(id='video')
    if not video:
        result['status'].append("Not found Video")
    else:
        result['poster_url'] = video.get('poster', '')
        source = video.find('source')
        if source:
            result['video_url'] = source.get('src', '')

    information = soup.find('div', {'class': 'information_box'})
    if information:
        for li in information.select('ul li'):
            key = li.select_one('.information-left')
            value = li.select_one('.information-right')
            if key and value:
                key_text = key.get_text(" ", strip=True).replace("：", "")
                value_text = value.get_text(" ", strip=True)
                result['information'][key_text] = value_text
                if key_text == 'タイトル':
                    result['title'] = value_text
    return result

def getSpank(soup):
    result = {'title': 'Unknown', 'status': [], 'information': {}}
    main = soup.find('main')
    if not main:
        result['status'].append("Not found 'main' tag")
        return result
    script = main.find('script')
    if script:
        urls_re = re.search(r'var stream_data = ({[^\n]+})', script.prettify())
        if urls_re:
            try:
                urls = ast.literal_eval(urls_re.group(1).strip())
                if 'main' in urls and urls['main']:
                    result['video_url'] = urls['main'][0]
                if 'cover_image' in urls:
                    result['poster_url'] = urls['cover_image']
            except:
                result['status'].append("Can't parse stream_data")
    video = soup.find(id='video')
    if video and video.find('h1'):
        result['title'] = video.find('h1').text.strip()
    return result

def minProcess(url):
    url = url.strip()
    sitenameRe = r'^https?://([^/]+)'
    if not re.match(sitenameRe, url):
        return {'ok': False, 'status': '不正なURLです'}
    
    sitename = re.match(sitenameRe, url).group(1)
    site = siteList.get(sitename, {'name': 'other', 'code': 10})

    try:
        # ご指定通りすべてのサイトをSelenium経由でHTML取得
        html_text = getBySelenium(url)
        soup = BeautifulSoup(html_text, 'html.parser')
        
        if site['name'] == 'zozo':
            data = getZozo(soup)
        elif site['name'] == 'spank':
            data = getSpank(soup)
        else:
            data = {'title': 'Unknown', 'status': ['Unsupported site']}
            
        return {'ok': True, 'data': data, 'url': url}
    except Exception as e:
        return {'ok': False, 'status': f'解析エラー: {e}'}

# --- HTML テンプレート (フロントエンド) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EC Video Helper</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f9f9f9; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-top: 20px; }
        input[type="text"] { width: 100%; padding: 10px; box-sizing: border-box; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; }
        button { background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; width: 100%; font-size: 16px; }
        button:hover { background: #0056b3; }
        .download-btn { background: #28a745; margin-top: 15px; display: inline-block; text-align: center; text-decoration: none; color: white; padding: 12px; border-radius: 4px; width: 100%; box-sizing: border-box; font-weight: bold; }
        .download-btn:hover { background: #218838; }
        a { color: #007bff; word-break: break-all; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background: #f2f2f2; }
    </style>
</head>
<body>
    <h2>🎬 EC動画解析 & ダウンロード</h2>
    <form method="POST" action="/">
        <input type="text" name="url" placeholder="動画のURLを入力（zozo / spankbang）" value="{{ url or '' }}" required>
        <button type="submit">解析スタート</button>
    </form>

    {% if error %}
        <div class="card" style="color: red;">{{ error }}</div>
    {% endif %}

    {% if data %}
        <div class="card">
            <h3>🎵 {{ data.get('title', 'タイトル不明') }}</h3>
            
            {% if data.get('information') %}
                <h4>📋 作品情報</h4>
                <table>
                    {% for k, v in data['information'].items() %}
                    <tr><th>{{ k }}</th><td>{{ v }}</td></tr>
                    {% endfor %}
                </table>
            {% endif %}

            <h4>🔗 リンク (タッチで開けます)</h4>
            <p>・元ページ: <a href="{{ url }}" target="_blank">{{ url }}</a></p>
            
            {% if data.get('video_url') %}
                <p>・直接動画: <a href="{{ data['video_url'] }}" target="_blank">ブラウザで動画を開く</a></p>
                <a class="download-btn" href="/download?video_url={{ data['video_url'] | urlencode }}">📥 この動画をダウンロード (MP4保存)</a>
            {% else %}
                <p style="color: orange;">⚠️ 動画URLの解析に失敗したか、ページ内に見つかりませんでした。</p>
            {% endif %}
        </div>
    {% endif %}
</body>
</html>
"""

# --- ルーター設定 ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        url = request.form.get('url')
        res = minProcess(url)
        if res['ok']:
            return render_template_string(HTML_TEMPLATE, data=res['data'], url=url)
        else:
            return render_template_string(HTML_TEMPLATE, error=res['status'], url=url)
    return render_template_string(HTML_TEMPLATE)

@app.route('/download')
def download():
    """バックエンド経由で大容量のMP4をストリーミングダウンロードさせる"""
    video_url = request.args.get('video_url')
    if not video_url:
        return "URLが指定されていません", 400

    parsed_url = urlparse(video_url)
    filename = pathlib.Path(parsed_url.path).name
    if not filename.endswith('.mp4'):
        filename = "video.mp4"

    try:
        # stream=True でチャンクごとにデータを読み込んでクライアントへ即時転送
        req = requests.get(video_url, stream=True)
        req.raise_for_status()

        def generate():
            for chunk in req.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(generate()),
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "video/mp4"
            }
        )
    except Exception as e:
        return f"ダウンロード中にエラーが発生しました: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

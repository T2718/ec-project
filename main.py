import os
import re
import ast
import pathlib
import datetime
import asyncio
from urllib.parse import urlparse
from quart import Quart, request, render_template_string, Response, stream_with_context
import httpx
from bs4 import BeautifulSoup

# Selenium関連
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

app = Quart(__name__)

siteList = {
    'zozovideo.com': {'name': 'zozo', 'code': 0},
    'jp.spankbang.com': {'name': 'spank', 'code': 1}
}

# 直接ダウンロードを許可する一般的な動画拡張子
VIDEO_EXTENSIONS = ('.mp4', '.m4v', '.webm', '.ogv', '.mov', '.avi', '.m3u8')

def get_headless_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

# 外部ブロックを防ぐため非同期でドライバーを実行
async def getBySeleniumAsync(url, queue):
    await queue.put("⚙️ ヘッドレスChromeを起動中...")
    loop = asyncio.get_event_loop()
    driver = await loop.run_in_executor(None, get_headless_driver)
    
    try:
        await queue.put(f"🌐 ターゲットサイトに接続中: {url}")
        await loop.run_in_executor(None, driver.get, url)
        
        await queue.put("📄 ページのレンダリング完了。HTMLを抽出しています...")
        html = await loop.run_in_executor(None, lambda: driver.page_source)
    finally:
        await queue.put("🧹 ブラウザを安全に閉じています...")
        await loop.run_in_executor(None, driver.quit)
    return html

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

# --- WEB UI (リアルタイム進捗対応) ---
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
        #progress-box { display: none; background: #e9ecef; border-left: 4px solid #007bff; padding: 12px; margin-top: 20px; border-radius: 4px; font-size: 14px; color: #495057; }
        #result-container { margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background: #f2f2f2; }
    </style>
</head>
<body>
    <h2>🎬 EC動画解析 & ダウンロード (非同期)</h2>
    <div>
        <input type="text" id="url-input" placeholder="動画のURLを入力（zozo / spankbang / 直接動画URL）" required>
        <button type="button" id="start-btn">解析スタート</button>
    </div>

    <div id="progress-box">⏳ 進捗ステータス待ち...</div>
    <div id="result-container"></div>

    <script>
        document.getElementById('start-btn').addEventListener('click', function() {
            const url = document.getElementById('url-input').value.trim();
            if (!url) return alert('URLを入力してください');

            const progressBox = document.getElementById('progress-box');
            const resultContainer = document.getElementById('result-container');
            
            progressBox.style.display = 'block';
            progressBox.innerText = '🚀 サーバーへ解析要求を送信中...';
            resultContainer.innerHTML = '';

            const eventSource = new EventSource('/analyze?url=' + encodeURIComponent(url));

            eventSource.onmessage = function(event) {
                const data = JSON.parse(event.data);
                
                if (data.type === 'progress') {
                    progressBox.innerText = data.message;
                } 
                else if (data.type === 'success') {
                    progressBox.innerText = '✅ 解析が完了しました！';
                    renderResult(data.data, url);
                    eventSource.close();
                } 
                else if (data.type === 'error') {
                    progressBox.style.display = 'none';
                    resultContainer.innerHTML = `<div class="card" style="color: red;">❌ エラー: ${data.message}</div>`;
                    eventSource.close();
                }
            };

            eventSource.onerror = function() {
                progressBox.innerText = '⚠️ 通信中にエラーが発生しました。';
                eventSource.close();
            };
        });

        function renderResult(data, originalUrl) {
            let infoRows = '';
            if (data.information) {
                for (const [k, v] of Object.entries(data.information)) {
                    infoRows += `<tr><th>${k}</th><td>${v}</td></tr>`;
                }
            }

            let html = `
                <div class="card">
                    <h3>🎵 ${data.title || 'タイトル不明'}</h3>
                    ${infoRows ? `<h4>📋 作品情報</h4><table>${infoRows}</table>` : ''}
                    <h4>🔗 リンク</h4>
                    <p>・元ページ: <a href="${originalUrl}" target="_blank">${originalUrl}</a></p>
            `;

            if (data.video_url) {
                html += `
                    <p>・直接動画: <a href="${data.video_url}" target="_blank">ブラウザで動画を開く</a></p>
                    <a class="download-btn" href="/download?video_url=${encodeURIComponent(data.video_url)}">📥 この動画をダウンロード (MP4保存)</a>
                `;
            } else {
                html += `<p style="color: orange;">⚠️ 動画URLの解析に失敗したか、ページ内に見つかりませんでした。</p>`;
            }

            html += `</div>`;
            document.getElementById('result-container').innerHTML = html;
        }
    </script>
</body>
</html>
"""

@app.route('/')
async def index():
    return await render_template_string(HTML_TEMPLATE)

@app.route('/analyze')
async def analyze():
    url = request.args.get('url', '').strip()
    
    async def generate_progress():
        if not url:
            # シングルクォーテーションで囲むことで、内部のダブルクォーテーションのエスケープを不要に
            yield f'data: {{"type": "error", "message": "URLが空です"}}\n\n'
            return

        sitenameRe = r'^https?://([^/]+)'
        if not re.match(sitenameRe, url):
            yield f'data: {{"type": "error", "message": "不正なURL構造です"}}\n\n'
            return

        parsed_url = urlparse(url)
        sitename = parsed_url.netloc
        site = siteList.get(sitename, {'name': 'other'})

        if parsed_url.path.lower().endswith(VIDEO_EXTENSIONS) or site['name'] == 'other':
            if parsed_url.path.lower().endswith(VIDEO_EXTENSIONS):
                yield f'data: {{"type": "progress", "message": "⚡ 直接動画URLを検出しました。解析をスキップします..."}}\n\n'
                filename = pathlib.Path(parsed_url.path).name or "direct_video.mp4"
                direct_data = {
                    'title': filename,
                    'video_url': url,
                    'information': {'ファイル名': filename, 'タイプ': '直接動画リンク'}
                }
                import json
                yield f'data: {{"type": "success", "data": {json.dumps(direct_data)}}}\n\n'
                return

        # スレッドセーフな非同期キューで進捗メッセージを受け渡す
        queue = asyncio.Queue()

        async def run_scraper():
            try:
                html_text = await getBySeleniumAsync(url, queue)
                await queue.put("🔍 解析用スープを作成中 (BeautifulSoup)...")
                soup = BeautifulSoup(html_text, 'html.parser')
                
                await queue.put("⚡ ターゲットデータを抽出中...")
                if site['name'] == 'zozo':
                    data = getZozo(soup)
                elif site['name'] == 'spank':
                    data = getSpank(soup)
                else:
                    data = {'title': 'Unknown', 'status': ['Unsupported site']}
                
                await queue.put(('SUCCESS', data))
            except Exception as e:
                await queue.put(('ERROR', str(e)))

        # スクレイピングタスクをバックグラウンドで開始
        scraper_task = asyncio.create_task(run_scraper())

        # キューから進捗状況を取り出して逐次クライアントへ送信
        while True:
            msg = await queue.get()
            if isinstance(msg, tuple):
                status_type, payload = msg
                if status_type == 'SUCCESS':
                    import json
                    yield f'data: {{"type": "success", "data": {json.dumps(payload)}}}\n\n'
                else:
                    yield f'data: {{"type": "error", "message": "{payload}"}}\n\n'
                break
            else:
                yield f'data: {{"type": "progress", "message": "{msg}"}}\n\n'

    return Response(generate_progress(), content_type='text/event-stream')

@app.route('/download')
async def download():
    video_url = request.args.get('video_url')
    if not video_url:
        return "URLが指定されていません", 400

    filename = pathlib.Path(urlparse(video_url).path).name or "video.mp4"

    try:
        async def stream_download():
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("GET", video_url) as r:
                    r.raise_for_status()
                    async with r.iter_bytes(chunk_size=8192) as chunks: # 非同期イテレータの安全な呼び出し
                        async for chunk in chunks:
                            yield chunk

        return Response(
            stream_with_context(stream_download()),
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "Content-Type": "video/mp4"
            }
        )
    except Exception as e:
        return f"ダウンロードエラー: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

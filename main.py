import os
import re
import ast
import json
import pathlib
import itertools
import asyncio
from urllib.parse import urlparse, quote
from quart import Quart, request, render_template_string, Response, stream_with_context
import httpx
import aiofiles
from bs4 import BeautifulSoup

# Selenium関連
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

app = Quart(__name__)

# ダウンロード保存用ディレクトリ
DOWNLOAD_DIR = pathlib.Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

siteList = {
    'zozovideo.com': {'name': 'zozo', 'code': 0},
    'jp.spankbang.com': {'name': 'spank', 'code': 1}
}

VIDEO_EXTENSIONS = ('.mp4', '.m4v', '.webm', '.ogv', '.mov', '.avi', '.m3u8')

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


# --- Selenium関連ヘルパー ---

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


# --- 各サイト専用スクレイピング ---

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
            except Exception:
                result['status'].append("Can't parse stream_data")
    video = soup.find(id='video')
    if video and video.find('h1'):
        result['title'] = video.find('h1').text.strip()
    return result


# --- HTML階層ツリー変換 ---

def _attrs_to_dict(node):
    attrs = {}
    for k, v in (node.attrs or {}).items():
        if isinstance(v, list):
            v = ' '.join(v)
        attrs[k] = v
    return attrs


def parse_html_to_tree(html_text):
    soup = BeautifulSoup(html_text, 'html.parser')
    counter = itertools.count(0)

    def build(node):
        if not hasattr(node, 'name') or node.name is None:
            return None
        node_id = next(counter)
        own_text_parts = []
        children = []
        for child in node.children:
            if isinstance(child, str):
                t = child.strip()
                if t:
                    own_text_parts.append(t)
            else:
                built = build(child)
                if built:
                    children.append(built)
        return {
            'id': node_id,
            'tag': node.name,
            'attrs': _attrs_to_dict(node),
            'text': ' '.join(own_text_parts)[:300],
            'children': children,
        }

    root_children = []
    for child in soup.children:
        if not isinstance(child, str):
            built = build(child)
            if built:
                root_children.append(built)

    return {'id': -1, 'tag': '#document', 'attrs': {}, 'text': '', 'children': root_children}


def find_selector_ids(html_text, selector):
    soup = BeautifulSoup(html_text, 'html.parser')
    counter = itertools.count(0)

    matched_objs = set()
    for el in soup.select(selector):
        matched_objs.add(id(el))

    matched_ids = []

    def walk(node):
        if not hasattr(node, 'name') or node.name is None:
            return
        node_id = next(counter)
        if id(node) in matched_objs:
            matched_ids.append(node_id)
        for child in node.children:
            if not isinstance(child, str):
                walk(child)

    for child in soup.children:
        if not isinstance(child, str):
            walk(child)

    return matched_ids


# --- WEB UI テンプレート ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>EC Video Helper</title>
    <style>
      body {
        font-family: Arial, sans-serif;
        max-width: 650px;
        margin: 40px auto;
        padding: 20px;
        background: #f9f9f9;
      }
      .card {
        background: white;
        padding: 20px;
        border-radius: 8px;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        margin-top: 20px;
      }
      input[type="text"] {
        width: 100%;
        padding: 10px;
        box-sizing: border-box;
        margin-bottom: 10px;
        border: 1px solid #ccc;
        border-radius: 4px;
      }
      .btn-group {
        display: flex;
        gap: 10px;
      }
      button {
        background: #007bff;
        color: white;
        border: none;
        padding: 10px 20px;
        border-radius: 4px;
        cursor: pointer;
        width: 100%;
        font-size: 15px;
      }
      button:hover { background: #0056b3; }
      button.secondary { background: #6c757d; }
      button.secondary:hover { background: #5a6268; }
      .download-btn {
        background: #28a745;
        margin-top: 15px;
        display: inline-block;
        text-align: center;
        text-decoration: none;
        color: white;
        padding: 12px;
        border-radius: 4px;
        width: 100%;
        box-sizing: border-box;
        font-weight: bold;
      }
      .download-btn:hover { background: #218838; }
      #progress-box {
        display: none;
        background: #e9ecef;
        border-left: 4px solid #007bff;
        padding: 12px;
        margin-top: 20px;
        border-radius: 4px;
        font-size: 14px;
        color: #495057;
        white-space: pre-wrap;
      }
      #result-container { margin-top: 20px; }
      table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 10px;
      }
      th, td {
        border: 1px solid #ddd;
        padding: 8px;
        text-align: left;
      }
      th { background: #f2f2f2; }

      /* ===== 開発パネル ===== */
      #dev-btn {
        position: fixed;
        top: 10px;
        left: 10px;
        z-index: 2000;
        width: auto;
        padding: 8px 14px;
        background: #343a40;
        font-size: 14px;
        border-radius: 4px;
      }
      #dev-btn:hover { background: #23272b; }
      #dev-panel {
        display: none;
        position: fixed;
        top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0, 0, 0, 0.5);
        z-index: 3000;
        align-items: center;
        justify-content: center;
      }
      .dev-panel-inner {
        background: #fff;
        width: 95%; height: 90%;
        max-width: 1100px;
        border-radius: 8px;
        display: flex;
        flex-direction: column;
        overflow: hidden;
      }
      .dev-header {
        display: flex;
        gap: 8px;
        padding: 12px;
        border-bottom: 1px solid #ddd;
        align-items: center;
      }
      .dev-header input[type="text"] { flex: 1; margin: 0; }
      .dev-header button { width: auto; padding: 8px 14px; white-space: nowrap; }
      .dev-close-btn { background: #6c757d; }
      .dev-close-btn:hover { background: #545b62; }
      .dev-status { padding: 4px 12px; font-size: 13px; color: #555; min-height: 20px; }
      .dev-search-bar {
        display: flex;
        gap: 8px;
        padding: 8px 12px;
        border-bottom: 1px solid #ddd;
        align-items: center;
        flex-wrap: wrap;
      }
      .dev-search-bar select { padding: 6px; border-radius: 4px; border: 1px solid #ccc; }
      .dev-search-bar input[type="text"] { flex: 1; min-width: 150px; margin: 0; padding: 6px; }
      .dev-search-bar button { width: auto; padding: 6px 12px; font-size: 13px; }
      #search-counter { font-size: 13px; color: #333; min-width: 60px; text-align: center; }
      .tree-container {
        flex: 1;
        overflow: auto;
        padding: 10px 14px;
        font-family: "Consolas", "Menlo", monospace;
        font-size: 13px;
        background: #fafafa;
      }
      .tree-node { margin-left: 16px; }
      .tree-node:first-child { margin-left: 0; }
      .node-line {
        cursor: pointer;
        padding: 1px 4px;
        border-radius: 3px;
        white-space: pre-wrap;
        word-break: break-all;
      }
      .node-line:hover { background: #eef2f7; }
      .toggle { display: inline-block; width: 14px; color: #888; }
      .tag-open { color: #0b5394; }
      .attr-name { color: #a52a2a; }
      .attr-value { color: #1a7a1a; }
      .node-text { color: #333; margin-left: 6px; }
      .children { margin-left: 4px; border-left: 1px dashed #ccc; padding-left: 6px; }
      .hl-line { background: #fff3a0 !important; }
      .active-match { outline: 2px solid #ff6600; background: #ffd580 !important; }
    </style>
  </head>
  <body>
    <button id="dev-btn" type="button" onclick="openDevPanel()">🛠 開発</button>

    <div id="dev-panel">
      <div class="dev-panel-inner">
        <div class="dev-header">
          <input type="text" id="dev-url-input" placeholder="解析したいURLを入力">
          <button type="button" onclick="fetchInspect()">取得</button>
          <button type="button" class="dev-close-btn" onclick="closeDevPanel()">✕ 閉じる</button>
        </div>
        <div id="dev-status" class="dev-status"></div>

        <div class="dev-search-bar">
          <select id="search-mode">
            <option value="tag">タグ名</option>
            <option value="text">文字列</option>
            <option value="selector">CSSクエリ</option>
          </select>
          <input type="text" id="search-input" placeholder="検索キーワード / セレクタ" onkeydown="if(event.key==='Enter'){ runSearch(); }">
          <button type="button" onclick="runSearch()">検索</button>
          <button type="button" onclick="gotoMatch(matchIndex - 1)">◀ 前へ</button>
          <span id="search-counter">0 / 0</span>
          <button type="button" onclick="gotoMatch(matchIndex + 1)">次へ ▶</button>
        </div>

        <div id="tree-container" class="tree-container"></div>
      </div>
    </div>

    <h2>🎬 EC動画解析 & ダウンロード</h2>
    <div>
      <input type="text" id="url-input" placeholder="動画URLを入力 (zozo / spankbang / YouTube / 各種Web動画)" required>
      <div class="btn-group">
        <button type="button" id="start-btn">通常解析 (Selenium)</button>
        <button type="button" id="ytdlp-btn" class="secondary">汎用解析 (yt-dlp)</button>
      </div>
    </div>

    <div id="progress-box">⏳ 進捗ステータス待ち...</div>
    <div id="result-container"></div>

    <script>
      // ===================== イベントハンドラ =====================
      document.getElementById('start-btn').addEventListener('click', () => executeAnalyze('/analyze?url='));
      document.getElementById('ytdlp-btn').addEventListener('click', () => executeAnalyze('/yt-dlp?action=info&url='));

      function executeAnalyze(endpointPrefix) {
        const url = document.getElementById('url-input').value.trim();
        if (!url) return alert('URLを入力してください');

        const progressBox = document.getElementById('progress-box');
        const resultContainer = document.getElementById('result-container');

        progressBox.style.display = 'block';
        progressBox.innerText = '🚀 サーバーへ解析要求を送信中...';
        resultContainer.innerHTML = '';

        const eventSource = new EventSource(endpointPrefix + encodeURIComponent(url));

        eventSource.onmessage = function (event) {
          const data = JSON.parse(event.data);

          if (data.type === 'progress') {
            progressBox.innerText = data.message;
          } else if (data.type === 'success') {
            progressBox.innerText = '✅ 解析が完了しました！';
            renderResult(data.data, url);
            eventSource.close();
          } else if (data.type === 'error') {
            progressBox.style.display = 'none';
            resultContainer.innerHTML = `<div class="card" style="color: red;">❌ エラー: ${data.message}</div>`;
            eventSource.close();
          }
        };

        eventSource.onerror = function () {
          progressBox.innerText = '⚠️ 通信エラーが発生しました。';
          eventSource.close();
        };
      }

      function downloadWithYtdlp(targetUrl) {
        const progressBox = document.getElementById('progress-box');
        progressBox.style.display = 'block';
        progressBox.innerText = '🚀 yt-dlpのダウンロード処理を開始します...';

        const eventSource = new EventSource('/yt-dlp?action=download&url=' + encodeURIComponent(targetUrl));

        eventSource.onmessage = function (event) {
          const data = JSON.parse(event.data);

          if (data.type === 'progress') {
            progressBox.innerText = data.message;
          } else if (data.type === 'success') {
            progressBox.innerText = '✅ サーバーでのダウンロードと結合が完了しました！';
            if (data.data.file_url) {
              window.location.href = data.data.file_url;
            }
            eventSource.close();
          } else if (data.type === 'error') {
            alert('ダウンロード失敗: ' + data.message);
            eventSource.close();
          }
        };

        eventSource.onerror = function () {
          progressBox.innerText = '⚠️ 通信エラーが発生しました。';
          eventSource.close();
        };
      }

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
            ${data.thumbnail ? `<img src="${data.thumbnail}" style="max-width:100%; border-radius:4px; margin-bottom:10px;">` : ''}
            ${infoRows ? `<h4>📋 作品情報</h4><table>${infoRows}</table>` : ''}
            <h4>🔗 リンク & ダウンロード</h4>
            <p>・元ページ: <a href="${originalUrl}" target="_blank">${originalUrl}</a></p>
        `;

        if (data.video_url) {
          html += `
            <p>・直接動画: <a href="${data.video_url}" target="_blank">ブラウザで動画を開く</a></p>
            <a class="download-btn" href="/download?video_url=${encodeURIComponent(data.video_url)}">📥 直接ストリーム保存 (MP4)</a>
          `;
        } else {
          html += `
            <button class="download-btn" onclick="downloadWithYtdlp('${originalUrl}')">📥 yt-dlp でサーバー経由取得・保存</button>
          `;
        }

        html += `</div>`;
        document.getElementById('result-container').innerHTML = html;
      }

      // ===================== 開発パネル機能 =====================
      let currentTree = null;
      let currentHtml = '';
      let idParentMap = {};
      let idNodeMap = {};
      let matches = [];
      let matchIndex = -1;

      function openDevPanel() { document.getElementById('dev-panel').style.display = 'flex'; }
      function closeDevPanel() { document.getElementById('dev-panel').style.display = 'none'; }

      function escapeHtml(str) {
        return String(str).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
      }

      function renderAttrs(attrs) {
        return Object.entries(attrs)
          .map(([k, v]) => ` <span class="attr-name">${escapeHtml(k)}</span>=<span class="attr-value">"${escapeHtml(v)}"</span>`)
          .join('');
      }

      function renderNode(node) {
        const hasChildren = node.children.length > 0;
        const toggleChar = hasChildren ? '▶' : ' ';
        const attrsHtml = renderAttrs(node.attrs);
        const textHtml = node.text ? `<span class="node-text">${escapeHtml(node.text)}</span>` : '';
        const childrenHtml = node.children.map(renderNode).join('');
        return `
          <div class="tree-node" id="node-${node.id}">
            <div class="node-line" onclick="toggleNode(event)">
              <span class="toggle">${toggleChar}</span>
              <span class="tag-open">&lt;${escapeHtml(node.tag)}${attrsHtml}&gt;</span>
              ${textHtml}
            </div>
            <div class="children" style="display:none;">${childrenHtml}</div>
          </div>`;
      }

      function toggleNode(event) {
        event.stopPropagation();
        const line = event.currentTarget;
        const wrapper = line.parentElement;
        const childrenBox = wrapper.querySelector(':scope > .children');
        const toggle = line.querySelector('.toggle');
        if (!childrenBox || childrenBox.children.length === 0) return;
        const isOpen = childrenBox.style.display !== 'none';
        childrenBox.style.display = isOpen ? 'none' : 'block';
        if (toggle.textContent.trim()) { toggle.textContent = isOpen ? '▶' : '▼'; }
      }

      function buildMaps(node, parentId) {
        idNodeMap[node.id] = node;
        idParentMap[node.id] = parentId;
        node.children.forEach(c => buildMaps(c, node.id));
      }

      async function fetchInspect() {
        const url = document.getElementById('dev-url-input').value.trim();
        const statusEl = document.getElementById('dev-status');
        const treeContainer = document.getElementById('tree-container');
        if (!url) return alert('URLを入力してください');

        statusEl.textContent = '⏳ 取得中...';
        treeContainer.innerHTML = '';
        matches = []; matchIndex = -1;
        idParentMap = {}; idNodeMap = {};
        updateCounter();

        try {
          const res = await fetch('/inspect/fetch?url=' + encodeURIComponent(url));
          const data = await res.json();
          if (!data.ok) {
            statusEl.textContent = '❌ ' + data.message;
            return;
          }
          currentTree = data.tree;
          currentHtml = data.html;
          currentTree.children.forEach(c => buildMaps(c, -1));
          treeContainer.innerHTML = currentTree.children.map(renderNode).join('');
          statusEl.textContent = '✅ 取得完了（要素数: ' + Object.keys(idNodeMap).length + '）';
        } catch (e) {
          statusEl.textContent = '❌ 通信エラー: ' + e;
        }
      }

      function nodeMatchesTag(node, q) { return node.tag.toLowerCase() === q.toLowerCase(); }
      function nodeMatchesText(node, q) {
        const ql = q.toLowerCase();
        if (node.tag.toLowerCase().includes(ql)) return true;
        if (node.text && node.text.toLowerCase().includes(ql)) return true;
        for (const k in node.attrs) {
          if (k.toLowerCase().includes(ql) || String(node.attrs[k]).toLowerCase().includes(ql)) return true;
        }
        return false;
      }

      function collectMatches(node, predicate, out) {
        if (predicate(node)) out.push(node.id);
        node.children.forEach(c => collectMatches(c, predicate, out));
      }

      async function queryBySelector(selector) {
        try {
          const res = await fetch('/inspect/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ html: currentHtml, selector: selector })
          });
          const data = await res.json();
          if (!data.ok) { alert(data.message || 'クエリエラー'); return []; }
          return data.matched_ids;
        } catch (e) {
          alert('クエリ通信エラー: ' + e);
          return [];
        }
      }

      function clearHighlights() {
        document.querySelectorAll('.hl-line').forEach(el => el.classList.remove('hl-line'));
        document.querySelectorAll('.active-match').forEach(el => el.classList.remove('active-match'));
      }

      function updateCounter() {
        const el = document.getElementById('search-counter');
        el.textContent = matches.length === 0 ? '0 / 0' : (matchIndex + 1) + ' / ' + matches.length;
      }

      async function runSearch() {
        if (!currentTree) return alert('先にURLを取得してください');
        const mode = document.getElementById('search-mode').value;
        const q = document.getElementById('search-input').value.trim();

        clearHighlights();
        matches = []; matchIndex = -1;
        if (!q) { updateCounter(); return; }

        let ids = [];
        if (mode === 'tag') collectMatches(currentTree, n => nodeMatchesTag(n, q), ids);
        else if (mode === 'text') collectMatches(currentTree, n => nodeMatchesText(n, q), ids);
        else if (mode === 'selector') ids = await queryBySelector(q);

        matches = ids;
        matches.forEach(id => {
          const line = document.querySelector('#node-' + id + ' > .node-line');
          if (line) line.classList.add('hl-line');
        });

        updateCounter();
        if (matches.length > 0) gotoMatch(0);
      }

      function gotoMatch(i) {
        if (matches.length === 0) return;
        const prevActive = document.querySelector('.active-match');
        if (prevActive) prevActive.classList.remove('active-match');

        matchIndex = ((i % matches.length) + matches.length) % matches.length;
        const id = matches[matchIndex];

        let pid = idParentMap[id];
        while (pid !== undefined && pid !== -1) {
          const parentEl = document.getElementById('node-' + pid);
          if (parentEl) {
            const childrenBox = parentEl.querySelector(':scope > .children');
            const toggle = parentEl.querySelector(':scope > .node-line > .toggle');
            if (childrenBox) childrenBox.style.display = 'block';
            if (toggle && toggle.textContent === '▶') toggle.textContent = '▼';
          }
          pid = idParentMap[pid];
        }

        const line = document.querySelector('#node-' + id + ' > .node-line');
        if (line) {
          line.classList.add('active-match');
          line.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        updateCounter();
      }
    </script>
  </body>
</html>
"""


# --- ルーティング定義 ---

@app.route('/')
async def index():
    return await render_template_string(HTML_TEMPLATE)


# --- 開発パネルAPI ---

@app.route('/inspect/fetch')
async def inspect_fetch():
    url = request.args.get('url', '').strip()
    if not url or not re.match(r'^https?://', url):
        return {'ok': False, 'message': '有効なURLを指定してください'}, 400

    timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers=FETCH_HEADERS)
            r.raise_for_status()
            html_text = r.text
    except Exception as e:
        return {'ok': False, 'message': f'取得エラー: {e}'}, 500

    tree = parse_html_to_tree(html_text)
    return {'ok': True, 'html': html_text, 'tree': tree}


@app.route('/inspect/query', methods=['POST'])
async def inspect_query():
    data = await request.get_json(silent=True) or {}
    html_text = data.get('html', '')
    selector = (data.get('selector') or '').strip()

    if not html_text or not selector:
        return {'ok': False, 'message': '入力値が不正です'}, 400

    try:
        matched_ids = find_selector_ids(html_text, selector)
    except Exception as e:
        return {'ok': False, 'message': f'セレクタの実行失敗: {e}'}, 400

    return {'ok': True, 'matched_ids': matched_ids}


# --- Selenium等による標準動画解析 ---

@app.route('/analyze')
async def analyze():
    url = request.args.get('url', '').strip()

    def sse(event_type, **kwargs):
        payload = {"type": event_type, **kwargs}
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    async def generate_progress():
        try:
            if not url or not re.match(r'^https?://([^/]+)', url):
                yield sse("error", message="URL形式が正しくありません")
                return

            parsed_url = urlparse(url)
            sitename = parsed_url.netloc
            site = siteList.get(sitename, {'name': 'other'})

            if parsed_url.path.lower().endswith(VIDEO_EXTENSIONS):
                yield sse("progress", message="⚡ 直接動画URLを検出しました。解析をスキップします...")
                filename = pathlib.Path(parsed_url.path).name or "direct_video.mp4"
                direct_data = {
                    'title': filename,
                    'video_url': url,
                    'information': {'ファイル名': filename, 'タイプ': '直接動画リンク'}
                }
                yield sse("success", data=direct_data)
                return

            queue = asyncio.Queue()

            async def run_scraper():
                try:
                    html_text = await getBySeleniumAsync(url, queue)
                    await queue.put("🔍 BeautifulSoupで要素解析を開始...")
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

            asyncio.create_task(run_scraper())

            while True:
                msg = await queue.get()
                if isinstance(msg, tuple):
                    status_type, payload = msg
                    if status_type == 'SUCCESS':
                        yield sse("success", data=payload)
                    else:
                        yield sse("error", message=payload)
                    break
                else:
                    yield sse("progress", message=msg)

        except Exception as e:
            yield sse("error", message=f"サーバー内部エラー: {e}")

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(generate_progress(), headers=headers)


# --- yt-dlp による汎用動画取得ルート ---

@app.route('/yt-dlp')
async def yt_dlp_route():
    url = request.args.get('url', '').strip()
    action = request.args.get('action', 'info').strip()

    def sse(event_type, **kwargs):
        payload = {"type": event_type, **kwargs}
        return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    async def generate_stream():
        if not url or not re.match(r'^https?://', url):
            yield sse("error", message="不正なURLです")
            return

        # 1. 情報解析モード
        if action == 'info':
            yield sse("progress", message="🔍 yt-dlpでメタデータを抽出中...")
            cmd = ["yt-dlp", "-j", "--no-warnings", url]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                err_msg = stderr.decode('utf-8', errors='ignore')
                yield sse("error", message=f"yt-dlp解析エラー: {err_msg[:200]}")
                return

            try:
                info = json.loads(stdout.decode('utf-8'))
                result_data = {
                    'title': info.get('title', 'Unknown Title'),
                    'thumbnail': info.get('thumbnail', ''),
                    'webpage_url': info.get('webpage_url', url),
                    'information': {
                        'タイトル': info.get('title', 'Unknown'),
                        '投稿者': info.get('uploader', '不明'),
                        '再生時間': info.get('duration_string', '不明'),
                        'フォーマット': info.get('ext', 'mp4')
                    }
                }
                yield sse("success", data=result_data)
            except Exception as e:
                yield sse("error", message=f"JSON解析エラー: {e}")
            return

        # 2. ダウンロード処理モード
        elif action == 'download':
            yield sse("progress", message="🚀 yt-dlpのダウンロードを開始します...")
            output_template = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")

            cmd = [
                "yt-dlp",
                "-f", "b[ext=mp4]/b",
                "-o", output_template,
                "--newline",
                "--no-warnings",
                url
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            filename = None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode('utf-8', errors='ignore').strip()

                if line_str.startswith("[download]"):
                    yield sse("progress", message=f"📥 {line_str}")
                    if "Destination:" in line_str:
                        filename = line_str.split("Destination:", 1)[1].strip()
                    elif "has already been downloaded" in line_str:
                        filename = line_str.split("[download]", 1)[1].replace("has already been downloaded", "").strip()

                elif line_str.startswith("[Merger]") or line_str.startswith("[ExtractAudio]"):
                    yield sse("progress", message="⚙️ 映像と音声を結合中...")

            await process.wait()

            if process.returncode == 0:
                fn_param = pathlib.Path(filename).name if filename else ""
                yield sse("success", data={
                    "message": "完了しました！",
                    "file_url": f"/yt-dlp/file?path={quote(fn_param)}"
                })
            else:
                stderr_data = await process.stderr.read()
                err_msg = stderr_data.decode('utf-8', errors='ignore')
                yield sse("error", message=f"ダウンロード失敗: {err_msg[:200]}")

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(generate_stream(), headers=headers)


# --- ファイル配信プロキシ ---

@app.route('/download')
async def download():
    video_url = request.args.get('video_url')
    if not video_url:
        return "URLが未指定です", 400

    filename = pathlib.Path(urlparse(video_url).path).name or "video.mp4"
    headers = {**FETCH_HEADERS, "Referer": video_url}

    timeout = httpx.Timeout(connect=15.0, read=None, write=None, pool=None)
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")

    client = None
    r = None
    try:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport)
        req = client.build_request("GET", video_url, headers=headers)
        r = await client.send(req, stream=True)
        r.raise_for_status()
    except Exception as e:
        if r: await r.aclose()
        if client: await client.aclose()
        return f"通信エラー: {e}", 502

    @stream_with_context
    async def stream_download():
        try:
            async for chunk in r.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    response_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": r.headers.get("Content-Type", "video/mp4")
    }
    if "Content-Length" in r.headers:
        response_headers["Content-Length"] = r.headers["Content-Length"]

    return Response(stream_download(), headers=response_headers)


@app.route('/yt-dlp/file')
async def yt_dlp_file_route():
    filename = request.args.get('path', '').strip()
    file_path = DOWNLOAD_DIR / filename

    if not filename or not file_path.exists() or not file_path.is_file():
        return "ファイルが見つかりません", 404

    @stream_with_context
    async def stream_file():
        async with aiofiles.open(file_path, mode='rb') as f:
            while chunk := await f.read(65536):
                yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "video/mp4",
        "Content-Length": str(file_path.stat().st_size)
    }
    return Response(stream_file(), headers=headers)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

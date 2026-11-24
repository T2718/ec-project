import os
import re
import ast
import json
import time
import uuid
import hmac
import socket
import hashlib
import asyncio
import ipaddress
import mimetypes
import pathlib
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, urlencode

from quart import Quart, request, Response, redirect
import httpx
import aiofiles
from bs4 import BeautifulSoup

# Selenium関連 (元ファイルから継承)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


app = Quart(__name__)

BASE_DIR = pathlib.Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
CONFIG_PATH = BASE_DIR / "config.json"
TOKEN_PATH = BASE_DIR / "token.json"

# ---- 環境変数 ----
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DEFAULT_ROOT_PATH = os.environ.get("ROOT_PATH", "")

SECRET = (
    os.environ.get("SECRET_KEY")
    or hashlib.sha256((APP_PASSWORD + GOOGLE_CLIENT_SECRET + "vds").encode()).hexdigest()
).encode()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive"

LIBRARY_FILENAME = "library.json"
VIDEO_DIRNAME = "videos"
UPLOAD_CHUNK = 8 * 1024 * 1024  # 256KiBの倍数

siteList = {
    'zozovideo.com': {'name': 'zozo', 'code': 0},
    'jp.spankbang.com': {'name': 'spank', 'code': 1}
}

VIDEO_EXTENSIONS = ('.mp4', '.m4v', '.webm', '.ogv', '.mov', '.avi', '.m3u8')

FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

BG_TASKS = set()


# ======================================================================
# 設定ファイル
# ======================================================================

def load_config():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except Exception:
            cfg = {}
    if not cfg.get('root_path') and DEFAULT_ROOT_PATH:
        cfg['root_path'] = DEFAULT_ROOT_PATH
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name or '').strip().strip('.')
    return name[:120]


# ======================================================================
# 簡易パスワード認証
# ======================================================================

def auth_token():
    return hmac.new(SECRET, b"auth", hashlib.sha256).hexdigest()


@app.before_request
async def check_auth():
    if not APP_PASSWORD:
        return None
    if request.path == '/login':
        return None
    cookie = request.cookies.get('vds_auth', '')
    if hmac.compare_digest(cookie.encode(), auth_token().encode()):
        return None
    if request.path.startswith('/api') or request.path.startswith('/auth'):
        return {'error': 'login'}, 401
    return redirect('/login')


LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ログイン</title>
    <style>
      body {
        font-family: sans-serif;
        background: #12141a;
        color: #e8eaf0;
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        margin: 0;
      }
      form {
        background: #1c1f28;
        padding: 24px;
        border-radius: 12px;
        width: 280px;
      }
      input, button {
        width: 100%;
        box-sizing: border-box;
        padding: 10px;
        margin-top: 10px;
        border-radius: 6px;
        border: 1px solid #3a3f4d;
        font-size: 16px;
      }
      input {
        background: #12141a;
        color: #e8eaf0;
      }
      button {
        background: #4f8cff;
        color: #fff;
        border: none;
      }
      .err {
        color: #ff7b7b;
        font-size: 14px;
      }
    </style>
  </head>
  <body>
    <form method="post">
      <h3>🔒 ログイン</h3>
      <input type="password" name="password" placeholder="パスワード" autofocus>
      <div class="err">__MSG__</div>
      <button type="submit">ログイン</button>
    </form>
  </body>
</html>
"""


@app.route('/login', methods=['GET', 'POST'])
async def login():
    msg = ''
    if request.method == 'POST':
        form = await request.form
        pw = form.get('password', '')
        if APP_PASSWORD and hmac.compare_digest(pw.encode(), APP_PASSWORD.encode()):
            resp = redirect('/')
            resp.set_cookie('vds_auth', auth_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax')
            return resp
        msg = 'パスワードが違います'
    return Response(LOGIN_TEMPLATE.replace('__MSG__', msg), content_type='text/html; charset=utf-8')


# ======================================================================
# Google Drive クライアント (REST直叩き)
# ======================================================================

class DriveAuthError(Exception):
    pass


class Drive:
    def __init__(self):
        self.access_token = None
        self.expires_at = 0
        self.lock = asyncio.Lock()

    def get_refresh_token(self):
        env = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
        if env:
            return env
        if TOKEN_PATH.exists():
            try:
                return json.loads(TOKEN_PATH.read_text()).get('refresh_token', '')
            except Exception:
                return ''
        return ''

    def has_token(self):
        return bool(self.get_refresh_token())

    async def get_access_token(self):
        async with self.lock:
            if self.access_token and time.time() < self.expires_at - 60:
                return self.access_token
            rt = self.get_refresh_token()
            if not rt:
                raise DriveAuthError("Google Driveに未接続です。設定画面から認証してください。")
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(TOKEN_URL, data={
                    'client_id': GOOGLE_CLIENT_ID,
                    'client_secret': GOOGLE_CLIENT_SECRET,
                    'refresh_token': rt,
                    'grant_type': 'refresh_token',
                })
            if r.status_code != 200:
                raise DriveAuthError(f"トークン更新に失敗しました: {r.text[:200]}")
            j = r.json()
            self.access_token = j['access_token']
            self.expires_at = time.time() + int(j.get('expires_in', 3600))
            return self.access_token

    async def auth_headers(self):
        return {'Authorization': f'Bearer {await self.get_access_token()}'}

    async def api(self, method, url, **kw):
        extra = kw.pop('headers', {})
        for attempt in range(2):
            headers = {**(await self.auth_headers()), **extra}
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as c:
                r = await c.request(method, url, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:
                self.access_token = None
                continue
            break
        if r.status_code >= 400:
            raise RuntimeError(f"Drive API エラー {r.status_code}: {r.text[:300]}")
        return r

    @staticmethod
    def esc(s):
        return s.replace('\\', '\\\\').replace("'", "\\'")

    async def find_child(self, parent_id, name, folder=None):
        q = f"name = '{self.esc(name)}' and '{parent_id}' in parents and trashed = false"
        if folder is True:
            q += " and mimeType = 'application/vnd.google-apps.folder'"
        elif folder is False:
            q += " and mimeType != 'application/vnd.google-apps.folder'"
        r = await self.api('GET', f"{DRIVE_API}/files", params={
            'q': q, 'fields': 'files(id,name,mimeType,size)', 'pageSize': 1,
        })
        files = r.json().get('files', [])
        return files[0] if files else None

    async def find_or_create_folder(self, parent_id, name):
        f = await self.find_child(parent_id, name, folder=True)
        if f:
            return f['id']
        r = await self.api('POST', f"{DRIVE_API}/files", params={'fields': 'id'}, json={
            'name': name, 'parents': [parent_id], 'mimeType': 'application/vnd.google-apps.folder',
        })
        return r.json()['id']

    async def ensure_path(self, path):
        parent = 'root'
        for part in [p for p in path.strip('/').split('/') if p]:
            parent = await self.find_or_create_folder(parent, part)
        return parent

    async def list_children(self, parent_id):
        out = []
        token = None
        while True:
            params = {
                'q': f"'{parent_id}' in parents and trashed = false",
                'fields': 'nextPageToken,files(id,name,mimeType,size)',
                'pageSize': 1000,
            }
            if token:
                params['pageToken'] = token
            j = (await self.api('GET', f"{DRIVE_API}/files", params=params)).json()
            out.extend(j.get('files', []))
            token = j.get('nextPageToken')
            if not token:
                return out

    async def create_json(self, parent_id, name, obj):
        b = 'vds_boundary_7f3a'
        meta = json.dumps({'name': name, 'parents': [parent_id], 'mimeType': 'application/json'})
        body = (
            f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(obj, ensure_ascii=False)}\r\n--{b}--"
        ).encode('utf-8')
        r = await self.api('POST', f"{UPLOAD_API}/files", params={'uploadType': 'multipart', 'fields': 'id'},
                           headers={'Content-Type': f'multipart/related; boundary={b}'}, content=body)
        return r.json()['id']

    async def update_json(self, file_id, obj):
        await self.api('PATCH', f"{UPLOAD_API}/files/{file_id}", params={'uploadType': 'media'},
                       headers={'Content-Type': 'application/json; charset=UTF-8'},
                       content=json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    async def upload_file(self, path, name, parent_id, mime, progress_cb=None):
        size = path.stat().st_size
        meta = json.dumps({'name': name, 'parents': [parent_id]}).encode('utf-8')
        r = await self.api('POST', f"{UPLOAD_API}/files", params={'uploadType': 'resumable', 'fields': 'id,size'},
                           headers={
                               'Content-Type': 'application/json; charset=UTF-8',
                               'X-Upload-Content-Type': mime,
                               'X-Upload-Content-Length': str(size),
                           }, content=meta)
        session_url = r.headers['Location']
        result = None
        sent = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as c:
            async with aiofiles.open(path, 'rb') as f:
                while sent < size:
                    chunk = await f.read(UPLOAD_CHUNK)
                    end = sent + len(chunk) - 1
                    rr = None
                    for attempt in range(3):
                        try:
                            rr = await c.put(session_url, content=chunk, headers={
                                'Content-Length': str(len(chunk)),
                                'Content-Range': f'bytes {sent}-{end}/{size}',
                            })
                            break
                        except httpx.HTTPError:
                            if attempt == 2:
                                raise
                            await asyncio.sleep(2)
                    if rr.status_code in (200, 201):
                        result = rr.json()
                    elif rr.status_code != 308:
                        raise RuntimeError(f"アップロード失敗 {rr.status_code}: {rr.text[:200]}")
                    sent = end + 1
                    if progress_cb:
                        await progress_cb(sent, size)
        if not result:
            raise RuntimeError("アップロードが完了しませんでした")
        return result

    async def trash(self, file_id):
        await self.api('PATCH', f"{DRIVE_API}/files/{file_id}", json={'trashed': True})


drive = Drive()


# ======================================================================
# ライブラリ (リスト構造 library.json)
# ======================================================================

class Library:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.init_lock = asyncio.Lock()
        self.reset()

    def reset(self):
        self.root_path = None
        self.root_id = None
        self.videos_id = None
        self.file_id = None
        self.data = None

    async def ensure(self):
        path = load_config().get('root_path', '').strip()
        if not path:
            raise RuntimeError("ルートディレクトリのパスが未設定です。設定画面で指定してください。")
        if self.data is not None and self.root_path == path:
            return
        async with self.init_lock:
            if self.data is not None and self.root_path == path:
                return
            self.reset()
            root_id = await drive.ensure_path(path)
            videos_id = await drive.find_or_create_folder(root_id, VIDEO_DIRNAME)
            f = await drive.find_child(root_id, LIBRARY_FILENAME, folder=False)
            data = None
            file_id = None
            if f:
                file_id = f['id']
                r = await drive.api('GET', f"{DRIVE_API}/files/{file_id}", params={'alt': 'media'})
                try:
                    data = r.json()
                except Exception:
                    data = None
            if not isinstance(data, dict):
                data = {}
            data.setdefault('version', 1)
            data.setdefault('folders', [])
            data.setdefault('items', [])
            self.root_path, self.root_id, self.videos_id = path, root_id, videos_id
            self.file_id, self.data = file_id, data
            if file_id is None:
                await self.save()

    async def save(self):
        self.data['updated'] = now_iso()
        if self.file_id:
            await drive.update_json(self.file_id, self.data)
        else:
            self.file_id = await drive.create_json(self.root_id, LIBRARY_FILENAME, self.data)

    def get_item(self, item_id):
        return next((i for i in self.data['items'] if i['id'] == item_id), None)

    def add_folder(self, name):
        if name and name not in self.data['folders']:
            self.data['folders'].append(name)


library = Library()


@app.errorhandler(DriveAuthError)
async def handle_auth_error(e):
    return {'error': 'auth', 'message': str(e)}, 401


@app.errorhandler(RuntimeError)
async def handle_runtime_error(e):
    return {'error': str(e)}, 500


# ======================================================================
# Google OAuth
# ======================================================================

def base_url():
    env = os.environ.get('BASE_URL')
    if env:
        return env.rstrip('/')
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    return f"{proto}://{request.host}"


@app.route('/auth/login')
async def auth_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return 'GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET が未設定です', 500
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': base_url() + '/auth/callback',
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': auth_token()[:16],
    }
    return redirect(AUTH_URL + '?' + urlencode(params))


@app.route('/auth/callback')
async def auth_callback():
    if request.args.get('state', '') != auth_token()[:16]:
        return '不正なstateです', 400
    code = request.args.get('code', '')
    if not code:
        return f"認証エラー: {request.args.get('error', '不明')}", 400
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data={
            'code': code,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri': base_url() + '/auth/callback',
            'grant_type': 'authorization_code',
        })
    if r.status_code != 200:
        return f"トークン取得に失敗しました: {r.text[:300]}", 400
    j = r.json()
    rt = j.get('refresh_token', '')
    if rt:
        TOKEN_PATH.write_text(json.dumps({'refresh_token': rt}))
    drive.access_token = j.get('access_token')
    drive.expires_at = time.time() + int(j.get('expires_in', 3600))
    library.reset()
    page = f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>認証完了</title>
  </head>
  <body style="font-family:sans-serif;padding:20px;background:#12141a;color:#e8eaf0;">
    <h2>✅ Google Drive に接続しました</h2>
    <p>Renderの再起動後も接続を維持するには、環境変数 <b>GOOGLE_REFRESH_TOKEN</b> に次の値を設定してください。</p>
    <textarea readonly style="width:100%;height:90px;">{rt}</textarea>
    <p><a href="/" style="color:#4f8cff;">アプリへ戻る</a></p>
  </body>
</html>"""
    return Response(page, content_type='text/html; charset=utf-8')


# ======================================================================
# Selenium・手動解析 (元ファイルから継承)
# ======================================================================

def get_headless_driver():
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--user-agent=' + FETCH_HEADERS['User-Agent'])
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


async def getBySeleniumAsync(url, emit=None):
    async def say(m):
        if emit:
            await emit(m)

    await say("⚙️ ヘッドレスChromeを起動中...")
    loop = asyncio.get_event_loop()
    driver = await loop.run_in_executor(None, get_headless_driver)
    try:
        await say(f"🌐 ターゲットサイトに接続中: {url}")
        await loop.run_in_executor(None, driver.get, url)
        await say("📄 レンダリング完了。HTMLを抽出しています...")
        html = await loop.run_in_executor(None, lambda: driver.page_source)
    finally:
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
            except Exception:
                result['status'].append("Can't parse stream_data")
    video = soup.find(id='video')
    if video and video.find('h1'):
        result['title'] = video.find('h1').text.strip()
    return result


async def extract_by_selenium(url, site_name, emit=None):
    html = await getBySeleniumAsync(url, emit)
    soup = BeautifulSoup(html, 'html.parser')
    return getZozo(soup) if site_name == 'zozo' else getSpank(soup)


async def ytdlp_get_url(url):
    p = await asyncio.create_subprocess_exec(
        'yt-dlp', '-g', '-f', 'b[ext=mp4]/b', '--no-warnings', '--no-playlist', url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError("yt-dlp解析エラー: " + err.decode('utf-8', 'ignore')[:200])
    lines = out.decode('utf-8', 'ignore').split()
    if not lines:
        raise RuntimeError("yt-dlpが動画URLを返しませんでした")
    return lines[0]


# ======================================================================
# ダウンロード (サーバー一時領域へ)
# ======================================================================

def guess_ext(url, content_type=''):
    suffix = pathlib.Path(urlparse(url).path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS and suffix != '.m3u8':
        return suffix
    ct = content_type.split(';')[0].strip().lower()
    return mimetypes.guess_extension(ct) if ct.startswith('video/') and mimetypes.guess_extension(ct) else '.mp4'


async def download_direct(video_url, job, referer, emit):
    headers = dict(FETCH_HEADERS)
    if referer:
        headers['Referer'] = referer
    part = DOWNLOAD_DIR / f"{job}.part"
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
    last = 0
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        async with c.stream('GET', video_url, headers=headers) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length', 0) or 0)
            ctype = r.headers.get('Content-Type', '')
            done = 0
            async with aiofiles.open(part, 'wb') as f:
                async for chunk in r.aiter_bytes(65536):
                    await f.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 0.7:
                        last = time.time()
                        pct = f" ({done * 100 // total}%)" if total else ""
                        await emit(f"📥 ダウンロード中: {done / 1048576:.1f}MB{pct}")
    final = DOWNLOAD_DIR / f"{job}{guess_ext(video_url, ctype)}"
    part.rename(final)
    return final


async def download_ytdlp(url, job, emit):
    out = str(DOWNLOAD_DIR / f"{job}.%(ext)s")
    cmd = [
        "yt-dlp", "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4", "-o", out,
        "--newline", "--no-warnings", "--no-playlist", url,
    ]
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail = []
    last = 0
    while True:
        line = await p.stdout.readline()
        if not line:
            break
        s = line.decode('utf-8', 'ignore').strip()
        if not s:
            continue
        tail = (tail + [s])[-8:]
        if s.startswith('[download]') and time.time() - last > 0.7:
            last = time.time()
            await emit(f"📥 {s}")
        elif s.startswith('[Merger]'):
            await emit("⚙️ 映像と音声を結合中...")
    await p.wait()
    files = [f for f in DOWNLOAD_DIR.glob(f"{job}.*") if f.suffix not in ('.part', '.ytdl')]
    if p.returncode != 0 or not files:
        raise RuntimeError("yt-dlp失敗: " + ' / '.join(tail[-3:])[:300])
    return max(files, key=lambda f: f.stat().st_size)


async def save_pipeline(url, name, mode, folder, emit):
    if not re.match(r'^https?://', url):
        raise RuntimeError("不正なURLです（http/httpsのみ）")
    await emit("☁️ Google Drive に接続中...")
    await library.ensure()

    job = uuid.uuid4().hex[:8]
    parsed = urlparse(url)
    path = parsed.path.lower()
    site = siteList.get(parsed.netloc)
    guessed_title = ''
    tmp = None
    try:
        if path.endswith(VIDEO_EXTENSIONS) and not path.endswith('.m3u8'):
            await emit("⚡ 直接動画URLを検出しました")
            tmp = await download_direct(url, job, '', emit)
        else:
            if mode in ('auto', 'manual') and site:
                try:
                    await emit("🕵️ ページを解析して動画URLを抽出します...")
                    data = await extract_by_selenium(url, site['name'], emit)
                    if data.get('title') and data['title'] != 'Unknown':
                        guessed_title = data['title']
                    vurl = data.get('video_url')
                    if not vurl:
                        raise RuntimeError("動画URLが見つかりませんでした")
                    tmp = await download_direct(vurl, job, url, emit)
                except Exception as e:
                    if mode == 'manual':
                        raise
                    await emit(f"⚠️ 手動解析に失敗: {e}\nyt-dlp に切り替えます...")
                    tmp = None
            elif mode == 'manual':
                raise RuntimeError("このサイトは手動解析に未対応です")
            if tmp is None:
                tmp = await download_ytdlp(url, job, emit)

        ext = tmp.suffix or '.mp4'
        final_name = safe_name(name) or safe_name(guessed_title) or "video_" + datetime.now().strftime('%Y%m%d_%H%M%S')
        drive_name = final_name + ext
        mime = mimetypes.guess_type(drive_name)[0] or 'video/mp4'
        local_size = tmp.stat().st_size

        await emit(f"☁️ Drive へアップロード開始 ({local_size / 1048576:.1f}MB)")
        last = [0]

        async def prog(done, total):
            if time.time() - last[0] > 0.7 or done >= total:
                last[0] = time.time()
                await emit(f"☁️ アップロード中: {done * 100 // total}%")

        up = await drive.upload_file(tmp, drive_name, library.videos_id, mime, prog)

        item = {
            'id': uuid.uuid4().hex[:12],
            'name': final_name,
            'folder': folder,
            'fileId': up['id'],
            'mime': mime,
            'size': int(up.get('size', local_size)),
            'source': url,
            'added': now_iso(),
            'memo': '',
        }
        async with library.lock:
            library.add_folder(folder)
            library.data['items'].append(item)
            await emit("📝 リストを更新中 (library.json)...")
            await library.save()
        return item
    finally:
        for f in DOWNLOAD_DIR.glob(f"{job}*"):
            try:
                f.unlink()
            except Exception:
                pass


def sse(event_type, **kwargs):
    return f'data: {json.dumps({"type": event_type, **kwargs}, ensure_ascii=False)}\n\n'


@app.route('/api/save')
async def api_save():
    url = request.args.get('url', '').strip()
    name = request.args.get('name', '').strip()
    mode = request.args.get('mode', 'auto').strip()
    folder = request.args.get('folder', '').strip()
    queue = asyncio.Queue()

    async def emit(msg):
        await queue.put(('p', msg))

    async def work():
        try:
            item = await save_pipeline(url, name, mode, folder, emit)
            await queue.put(('s', item))
        except Exception as e:
            await queue.put(('e', str(e)))

    task = asyncio.create_task(work())
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)

    async def gen():
        while True:
            kind, payload = await queue.get()
            if kind == 'p':
                yield sse('progress', message=payload)
            elif kind == 's':
                yield sse('success', item=payload)
                break
            else:
                yield sse('error', message=payload)
                break

    return Response(gen(), headers=SSE_HEADERS)


# ======================================================================
# 保存せず視聴 (抽出 + プロキシ)
# ======================================================================

def is_public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


@app.route('/api/extract')
async def api_extract():
    url = request.args.get('url', '').strip()
    if not re.match(r'^https?://', url):
        return {'error': '不正なURLです'}, 400
    parsed = urlparse(url)
    site = siteList.get(parsed.netloc)
    title = ''
    referer = ''
    try:
        if parsed.path.lower().endswith(VIDEO_EXTENSIONS):
            video_url = url
        elif site:
            data = await extract_by_selenium(url, site['name'])
            video_url = data.get('video_url')
            title = data.get('title', '') if data.get('title') != 'Unknown' else ''
            referer = url
        else:
            video_url = await ytdlp_get_url(url)
            referer = url
    except Exception as e:
        return {'error': f'抽出に失敗しました: {e}'}, 422
    if not video_url:
        return {'error': '動画URLが見つかりませんでした'}, 422
    if '.m3u8' in video_url.lower():
        return {'error': 'HLS(m3u8)はブラウザ直接再生に未対応です。保存してから視聴してください。'}, 422
    return {
        'ok': True,
        'title': title,
        'video_url': video_url,
        'proxy': '/api/proxy?url=' + quote(video_url, safe='') + '&referer=' + quote(referer, safe=''),
    }


async def relay(url, headers, params=None, force_mime=None):
    client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=None), follow_redirects=True)
    try:
        req = client.build_request('GET', url, headers=headers, params=params)
        r = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        return f'接続エラー: {e}', 502
    if r.status_code >= 400:
        await r.aclose()
        await client.aclose()
        return f'配信元がエラーを返しました: {r.status_code}', 502
    out = {
        'Accept-Ranges': 'bytes',
        'Content-Type': force_mime or r.headers.get('Content-Type', 'video/mp4'),
    }
    for h in ('Content-Length', 'Content-Range'):
        if h in r.headers:
            out[h] = r.headers[h]

    async def gen():
        try:
            async for chunk in r.aiter_raw(65536):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return Response(gen(), status=r.status_code, headers=out)


@app.route('/api/proxy')
async def api_proxy():
    url = request.args.get('url', '').strip()
    referer = request.args.get('referer', '').strip()
    if not re.match(r'^https?://', url):
        return '不正なURLです', 400
    host = urlparse(url).hostname or ''
    ok = await asyncio.get_event_loop().run_in_executor(None, is_public_host, host)
    if not ok:
        return '許可されないホストです', 403
    headers = {**FETCH_HEADERS, 'Accept-Encoding': 'identity'}
    if referer:
        headers['Referer'] = referer
    if request.headers.get('Range'):
        headers['Range'] = request.headers['Range']
    return await relay(url, headers)


# ======================================================================
# Drive上の動画ストリーミング (Range対応)
# ======================================================================

@app.route('/api/stream/<item_id>')
async def api_stream(item_id):
    await library.ensure()
    item = library.get_item(item_id)
    if not item:
        return '見つかりません', 404
    headers = {**(await drive.auth_headers()), 'Accept-Encoding': 'identity'}
    if request.headers.get('Range'):
        headers['Range'] = request.headers['Range']
    return await relay(f"{DRIVE_API}/files/{item['fileId']}", headers, params={'alt': 'media'},
                       force_mime=item.get('mime') or 'video/mp4')


# ======================================================================
# ライブラリ API
# ======================================================================

@app.route('/api/config', methods=['GET', 'POST'])
async def api_config():
    cfg = load_config()
    if request.method == 'POST':
        body = await request.get_json(silent=True) or {}
        path = '/'.join(p for p in str(body.get('root_path', '')).strip().strip('/').split('/') if p)
        cfg['root_path'] = path
        save_config(cfg)
        library.reset()
    return {
        'root_path': cfg.get('root_path', ''),
        'drive_ready': drive.has_token(),
        'client_configured': bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        'password_set': bool(APP_PASSWORD),
    }


@app.route('/api/library')
async def api_library():
    await library.ensure()
    return {
        'ok': True,
        'root_path': library.root_path,
        'folders': library.data['folders'],
        'items': library.data['items'],
    }


@app.route('/api/library/reload', methods=['POST'])
async def api_library_reload():
    library.reset()
    await library.ensure()
    return {'ok': True}


@app.route('/api/items/<item_id>', methods=['PATCH', 'DELETE'])
async def api_item(item_id):
    await library.ensure()
    async with library.lock:
        item = library.get_item(item_id)
        if not item:
            return {'error': '見つかりません'}, 404
        if request.method == 'PATCH':
            body = await request.get_json(silent=True) or {}
            if 'name' in body and str(body['name']).strip():
                item['name'] = safe_name(str(body['name'])) or item['name']
            if 'folder' in body:
                item['folder'] = str(body['folder']).strip()
                library.add_folder(item['folder'])
            if 'memo' in body:
                item['memo'] = str(body['memo'])[:20000]
            await library.save()
            return {'ok': True, 'item': item}
        library.data['items'] = [i for i in library.data['items'] if i['id'] != item_id]
        if request.args.get('drive', '1') == '1':
            try:
                await drive.trash(item['fileId'])
            except Exception:
                pass
        await library.save()
        return {'ok': True}


@app.route('/api/folders', methods=['POST', 'DELETE'])
async def api_folders():
    await library.ensure()
    async with library.lock:
        if request.method == 'POST':
            body = await request.get_json(silent=True) or {}
            name = safe_name(str(body.get('name', '')))
            if not name:
                return {'error': 'フォルダ名が空です'}, 400
            library.add_folder(name)
        else:
            name = request.args.get('name', '')
            library.data['folders'] = [f for f in library.data['folders'] if f != name]
            for i in library.data['items']:
                if i.get('folder') == name:
                    i['folder'] = ''
        await library.save()
    return {'ok': True}


@app.route('/api/scan', methods=['POST'])
async def api_scan():
    await library.ensure()
    async with library.lock:
        known = {i['fileId'] for i in library.data['items']}
        added = 0
        for f in await drive.list_children(library.videos_id):
            if f['id'] in known or not f.get('mimeType', '').startswith('video/'):
                continue
            library.data['items'].append({
                'id': uuid.uuid4().hex[:12],
                'name': pathlib.Path(f['name']).stem,
                'folder': '',
                'fileId': f['id'],
                'mime': f['mimeType'],
                'size': int(f.get('size', 0) or 0),
                'source': '',
                'added': now_iso(),
                'memo': '',
            })
            added += 1
        if added:
            await library.save()
    return {'ok': True, 'added': added}


# ======================================================================
# WEB UI
# ======================================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Drive Video Library</title>
    <style>
      :root {
        --bg: #12141a;
        --panel: #1c1f28;
        --panel2: #252a36;
        --line: #343a49;
        --text: #e8eaf0;
        --sub: #9aa3b5;
        --accent: #4f8cff;
        --danger: #e5484d;
        --ok: #3ccf91;
      }
      * { box-sizing: border-box; }
      html, body { height: 100%; }
      body {
        margin: 0;
        background: var(--bg);
        color: var(--text);
        font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
        font-size: 15px;
      }
      button, select, input, textarea {
        font: inherit;
        color: var(--text);
        background: var(--panel2);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 8px 10px;
      }
      button { cursor: pointer; }
      button:hover { background: #2f3646; }
      button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
      button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
      button.ghost { background: transparent; }
      input[type="range"] { padding: 0; }
      header {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 14px;
        background: var(--panel);
        border-bottom: 1px solid var(--line);
      }
      header h1 { font-size: 17px; margin: 0; flex: 1; }
      #status-pill { font-size: 12px; color: var(--sub); }
      .layout {
        display: grid;
        grid-template-columns: 340px 1fr;
        height: calc(100% - 53px);
      }
      aside {
        border-right: 1px solid var(--line);
        display: flex;
        flex-direction: column;
        min-height: 0;
        background: var(--panel);
      }
      .side-tools {
        padding: 10px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        border-bottom: 1px solid var(--line);
      }
      .row { display: flex; gap: 6px; align-items: center; }
      .row > * { min-width: 0; }
      .grow { flex: 1; }
      #list { overflow: auto; flex: 1; padding: 6px; }
      .item {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px;
        border-radius: 8px;
        cursor: pointer;
      }
      .item:hover { background: var(--panel2); }
      .item.active { background: #2a3a5c; }
      .item .name {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .item .meta { font-size: 11px; color: var(--sub); }
      .item button { padding: 4px 8px; }
      main {
        overflow: auto;
        padding: 14px;
        min-width: 0;
      }
      .player-wrap {
        position: relative;
        background: #000;
        border-radius: 10px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        max-width: 1100px;
      }
      .stage {
        position: relative;
        aspect-ratio: 16 / 9;
        background: #000;
        flex: 1;
        min-height: 0;
      }
      .player-wrap:fullscreen { max-width: none; border-radius: 0; }
      .player-wrap:fullscreen .stage { aspect-ratio: auto; }
      video { width: 100%; height: 100%; display: block; background: #000; }
      #tap-layer { position: absolute; inset: 0; touch-action: manipulation; }
      .fx {
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        padding: 14px 18px;
        border-radius: 40px;
        background: rgba(0, 0, 0, 0.6);
        font-size: 18px;
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.2s;
      }
      .fx.show { opacity: 1; }
      #fx-left { left: 8%; }
      #fx-right { right: 8%; }
      #loading {
        position: absolute;
        inset: 0;
        background: rgba(0, 0, 0, 0.82);
        display: none;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 10px;
        padding: 20px;
        text-align: center;
      }
      #loading.show { display: flex; }
      .bar { width: min(420px, 80%); height: 10px; background: #333; border-radius: 6px; overflow: hidden; }
      .bar > div { height: 100%; width: 0; background: var(--accent); transition: width 0.15s; }
      .controls {
        background: var(--panel);
        padding: 8px 10px;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .controls .row { flex-wrap: wrap; }
      #seek { width: 100%; }
      #time { font-variant-numeric: tabular-nums; font-size: 13px; color: var(--sub); }
      #vol { width: 90px; }
      .title-row { margin: 12px 0 6px; display: flex; gap: 8px; align-items: center; }
      .title-row h2 { margin: 0; font-size: 18px; flex: 1; word-break: break-all; }
      .memo-box { margin-top: 12px; max-width: 1100px; }
      #memo { width: 100%; min-height: 130px; resize: vertical; line-height: 1.5; }
      .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
      .chip { background: #2a3a5c; border: none; padding: 4px 10px; border-radius: 14px; font-size: 13px; }
      .sub { color: var(--sub); font-size: 13px; }
      dialog {
        background: var(--panel);
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 12px;
        width: min(520px, 94vw);
        padding: 18px;
      }
      dialog::backdrop { background: rgba(0, 0, 0, 0.6); }
      dialog h3 { margin: 0 0 12px; }
      .field { margin-bottom: 10px; }
      .field label { display: block; font-size: 12px; color: var(--sub); margin-bottom: 4px; }
      .field input, .field select { width: 100%; }
      .progress-box {
        display: none;
        margin-top: 10px;
        padding: 10px;
        background: var(--panel2);
        border-left: 4px solid var(--accent);
        border-radius: 6px;
        font-size: 13px;
        white-space: pre-wrap;
        word-break: break-all;
      }
      #toast {
        position: fixed;
        left: 50%;
        bottom: 24px;
        transform: translateX(-50%);
        background: #000;
        color: #fff;
        padding: 10px 16px;
        border-radius: 8px;
        opacity: 0;
        transition: opacity 0.25s;
        pointer-events: none;
        z-index: 9999;
        max-width: 90vw;
      }
      #toast.show { opacity: 0.95; }
      @media (max-width: 820px) {
        .layout { grid-template-columns: 1fr; grid-template-rows: auto 1fr; height: auto; }
        aside { border-right: none; border-bottom: 1px solid var(--line); order: 2; max-height: none; }
        main { order: 1; padding: 10px; }
        #list { max-height: 50vh; }
      }
    </style>
  </head>
  <body>
    <header>
      <h1>🎬 Drive Video Library</h1>
      <span id="status-pill"></span>
      <button id="btn-add" class="primary">＋ 追加</button>
      <button id="btn-settings">⚙️</button>
    </header>

    <div class="layout">
      <aside>
        <div class="side-tools">
          <input type="search" id="search" placeholder="🔍 名前・メモを検索">
          <div class="row">
            <select id="folder-filter" class="grow"></select>
            <button id="btn-folder-add" title="フォルダ追加">📁＋</button>
            <button id="btn-folder-del" title="選択中のフォルダを削除">🗑</button>
          </div>
        </div>
        <div id="list"></div>
      </aside>

      <main>
        <div class="player-wrap" id="player-wrap">
          <div class="stage">
            <video id="video" playsinline preload="auto"></video>
            <div id="tap-layer"></div>
            <div class="fx" id="fx-left">⏪</div>
            <div class="fx" id="fx-right">⏩</div>
            <div id="loading">
              <div id="loading-text">読み込み中...</div>
              <div class="bar"><div id="loading-bar"></div></div>
              <button id="btn-stream">待たずにストリーミング再生</button>
            </div>
          </div>
          <div class="controls">
            <input type="range" id="seek" min="0" max="1000" value="0">
            <div class="row">
              <button id="btn-back">⏪</button>
              <button id="btn-play" class="primary">▶</button>
              <button id="btn-fwd">⏩</button>
              <span id="time">0:00 / 0:00</span>
              <span class="grow"></span>
              <button id="btn-mute">🔊</button>
              <input type="range" id="vol" min="0" max="1" step="0.05" value="1">
            </div>
            <div class="row">
              <label class="sub">速度</label>
              <select id="speed"></select>
              <button id="btn-loop">🔁 OFF</button>
              <button id="btn-pip">PiP</button>
              <button id="btn-fs">⛶</button>
              <span class="grow"></span>
              <span class="sub" id="skip-hint"></span>
            </div>
          </div>
        </div>

        <div class="title-row">
          <h2 id="cur-title">動画を選択してください</h2>
          <button id="btn-save-temp" style="display:none;">💾 保存</button>
        </div>
        <div class="sub" id="cur-meta">ダブルクリック／ダブルタップ: 左でー、右で＋スキップ、中央で全画面</div>

        <div class="memo-box">
          <div class="row">
            <b>📝 メモ</b>
            <button id="btn-stamp">⏱ 現在位置を挿入</button>
            <span class="grow"></span>
            <span class="sub" id="memo-status"></span>
          </div>
          <textarea id="memo" placeholder="この動画のメモ。[1:23] のように書くとタイムスタンプになります" disabled></textarea>
          <div class="chips" id="chips"></div>
        </div>
      </main>
    </div>

    <!-- 追加ダイアログ -->
    <dialog id="dlg-add">
      <h3>動画を追加</h3>
      <div class="field">
        <label>動画ページのURL / 直接動画URL</label>
        <input type="text" id="add-url" placeholder="https://...">
      </div>
      <div class="field">
        <label>保存名（空欄なら自動）</label>
        <input type="text" id="add-name">
      </div>
      <div class="row">
        <div class="field grow">
          <label>取得方法</label>
          <select id="add-mode">
            <option value="auto">自動（手動解析→yt-dlp）</option>
            <option value="ytdlp">yt-dlp のみ</option>
            <option value="manual">手動解析のみ (zozo / spankbang)</option>
          </select>
        </div>
        <div class="field grow">
          <label>フォルダ</label>
          <select id="add-folder"></select>
        </div>
      </div>
      <div class="row">
        <button id="add-save" class="primary grow">💾 Driveに保存</button>
        <button id="add-preview" class="grow">▶ 保存せず視聴</button>
        <button id="add-close">閉じる</button>
      </div>
      <div class="progress-box" id="add-progress"></div>
    </dialog>

    <!-- 編集ダイアログ -->
    <dialog id="dlg-edit">
      <h3>動画の編集</h3>
      <div class="field">
        <label>名前</label>
        <input type="text" id="edit-name">
      </div>
      <div class="field">
        <label>フォルダ</label>
        <select id="edit-folder"></select>
      </div>
      <div class="sub" id="edit-info" style="margin-bottom:10px;"></div>
      <div class="row">
        <button id="edit-save" class="primary grow">保存</button>
        <button id="edit-delete" class="danger">削除</button>
        <button id="edit-close">閉じる</button>
      </div>
    </dialog>

    <!-- 設定ダイアログ -->
    <dialog id="dlg-settings">
      <h3>設定</h3>
      <div class="field">
        <label>Google Drive</label>
        <div class="row">
          <span id="set-drive" class="grow"></span>
          <a href="/auth/login"><button type="button" class="primary">Googleで認証</button></a>
        </div>
      </div>
      <div class="field">
        <label>ルートディレクトリ（マイドライブ上のパス 例: Videos/Library）</label>
        <div class="row">
          <input type="text" id="set-root" class="grow" placeholder="Videos/Library">
          <button id="set-root-save" class="primary">適用</button>
        </div>
      </div>
      <div class="field">
        <label>スキップ秒数（ダブルクリック / ダブルタップ）</label>
        <input type="number" id="set-skip" min="1" max="300">
      </div>
      <div class="field">
        <label><input type="checkbox" id="set-full" style="width:auto;"> 視聴前に全て読み込む（どこから再生しても即開始）</label>
      </div>
      <div class="row" style="flex-wrap:wrap;">
        <button id="set-scan">Drive上の動画を取り込む</button>
        <button id="set-reload">リスト再読込</button>
        <span class="grow"></span>
        <button id="set-close">閉じる</button>
      </div>
      <p class="sub">リスト構造・メモはルート直下の library.json に保存されます。動画ファイルは videos フォルダに入ります。</p>
    </dialog>

    <div id="toast"></div>

    <script>
      const $ = (id) => document.getElementById(id);
      const S = {
        cfg: {},
        folders: [],
        items: [],
        current: null,
        currentUrl: '',
        blobUrl: null,
        abort: null,
        skip: parseInt(localStorage.getItem('skipSec')) || 10,
        fullLoad: localStorage.getItem('fullLoad') !== '0',
        filter: '__all__',
        query: ''
      };
      const video = $('video');

      function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
      }
      function fmt(t) {
        if (!isFinite(t)) t = 0;
        t = Math.floor(t);
        const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
        return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
      }
      function fmtSize(b) {
        if (!b) return '';
        if (b > 1073741824) return (b / 1073741824).toFixed(2) + 'GB';
        return (b / 1048576).toFixed(1) + 'MB';
      }
      let toastTimer = null;
      function toast(msg) {
        const el = $('toast');
        el.textContent = msg;
        el.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
      }

      async function api(path, opts = {}) {
        const init = { method: opts.method || 'GET', headers: { 'Content-Type': 'application/json' } };
        if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
        const r = await fetch(path, init);
        let j = null;
        try { j = await r.json(); } catch (e) {}
        if (r.status === 401) {
          if (j && j.error === 'login') { location.href = '/login'; }
          else { openSettings(); }
          throw new Error((j && j.message) || '認証が必要です');
        }
        if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
        return j;
      }

      // ===================== 初期化 =====================
      async function init() {
        buildSpeedOptions();
        applySavedPlayerState();
        try {
          const cfg = await api('/api/config');
          S.cfg = cfg;
          const saved = localStorage.getItem('rootPath');
          if (!cfg.root_path && saved) {
            S.cfg = await api('/api/config', { method: 'POST', body: { root_path: saved } });
          }
          updateStatus();
          if (!S.cfg.drive_ready || !S.cfg.root_path) { openSettings(); return; }
          await loadLibrary();
        } catch (e) {
          toast(e.message);
        }
      }

      function updateStatus() {
        $('status-pill').textContent = (S.cfg.drive_ready ? '☁️ ' : '⚠️未接続 ') + (S.cfg.root_path || '(ルート未設定)');
      }

      async function loadLibrary() {
        const j = await api('/api/library');
        S.folders = j.folders;
        S.items = j.items;
        if (S.current && !S.current.temp) {
          S.current = S.items.find((i) => i.id === S.current.id) || S.current;
        }
        renderFolders();
        renderList();
      }

      // ===================== リスト描画 =====================
      function renderFolders() {
        const sel = $('folder-filter');
        const opts = [['__all__', '📂 すべて'], ['__none__', '📄 未分類']].concat(S.folders.map((f) => [f, '📁 ' + f]));
        sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('');
        if (!opts.some(([v]) => v === S.filter)) S.filter = '__all__';
        sel.value = S.filter;
      }

      function folderOptionsHtml(selected) {
        return ['<option value="">未分類</option>'].concat(S.folders.map((f) =>
          `<option value="${esc(f)}" ${f === selected ? 'selected' : ''}>${esc(f)}</option>`)).join('');
      }

      function renderList() {
        const q = S.query.toLowerCase();
        const list = S.items.filter((i) => {
          const fOk = S.filter === '__all__' || (S.filter === '__none__' ? !i.folder : i.folder === S.filter);
          const qOk = !q || i.name.toLowerCase().includes(q) || (i.memo || '').toLowerCase().includes(q);
          return fOk && qOk;
        }).sort((a, b) => (b.added || '').localeCompare(a.added || ''));
        $('list').innerHTML = list.length ? list.map((i) => {
          const pos = parseFloat(localStorage.getItem('pos:' + i.id)) || 0;
          return `
            <div class="item ${S.current && S.current.id === i.id ? 'active' : ''}" data-id="${i.id}">
              <div class="name">
                ${esc(i.name)}
                <div class="meta">${esc(i.folder || '未分類')} ・ ${fmtSize(i.size)}${pos > 5 ? ' ・ ▶ ' + fmt(pos) : ''}</div>
              </div>
              ${i.memo ? '<span title="メモあり">📝</span>' : ''}
              <button class="ghost" data-act="edit">✏️</button>
            </div>`;
        }).join('') : '<div class="sub" style="padding:14px;">動画がありません。「＋ 追加」から保存してください。</div>';
      }

      $('list').addEventListener('click', (e) => {
        const row = e.target.closest('.item');
        if (!row) return;
        const id = row.dataset.id;
        if (e.target.closest('[data-act="edit"]')) { openEdit(id); return; }
        openItem(id);
      });
      $('search').addEventListener('input', (e) => { S.query = e.target.value; renderList(); });
      $('folder-filter').addEventListener('change', (e) => { S.filter = e.target.value; renderList(); });
      $('btn-folder-add').addEventListener('click', async () => {
        const name = prompt('新しいフォルダ名');
        if (!name) return;
        try { await api('/api/folders', { method: 'POST', body: { name } }); await loadLibrary(); } catch (e) { toast(e.message); }
      });
      $('btn-folder-del').addEventListener('click', async () => {
        if (S.filter === '__all__' || S.filter === '__none__') return toast('削除するフォルダを選択してください');
        if (!confirm(`フォルダ「${S.filter}」を削除しますか？（動画は未分類に移動します）`)) return;
        try {
          await api('/api/folders?name=' + encodeURIComponent(S.filter), { method: 'DELETE' });
          S.filter = '__all__';
          await loadLibrary();
        } catch (e) { toast(e.message); }
      });

      // ===================== 動画を開く =====================
      let memoTimer = null;
      async function flushMemo() {
        if (memoTimer) {
          clearTimeout(memoTimer);
          memoTimer = null;
          if (S.current && !S.current.temp) await saveMemo(S.current);
        }
      }

      function releaseBlob() {
        if (S.blobUrl) { URL.revokeObjectURL(S.blobUrl); S.blobUrl = null; }
      }

      async function openItem(id) {
        const item = S.items.find((i) => i.id === id);
        if (!item) return;
        await flushMemo();
        S.current = item;
        setMeta(item);
        renderList();
        await loadSource('/api/stream/' + item.id, item.mime);
      }

      async function openTemp(name, src, original) {
        await flushMemo();
        S.current = { temp: true, id: null, name, source: original };
        setMeta(S.current);
        renderList();
        await loadSource(src, 'video/mp4');
      }

      function setMeta(item) {
        $('cur-title').textContent = item.name;
        $('btn-save-temp').style.display = item.temp ? '' : 'none';
        $('cur-meta').textContent = item.temp
          ? '保存していない動画を視聴中（メモは保存後に使えます）'
          : `${item.folder || '未分類'} ・ ${fmtSize(item.size)} ・ 追加: ${(item.added || '').slice(0, 10)}`;
        $('memo').disabled = !!item.temp;
        $('memo').value = item.temp ? '' : (item.memo || '');
        $('memo-status').textContent = '';
        renderChips();
      }

      function showLoading(ratio, text) {
        $('loading').classList.add('show');
        $('loading-text').textContent = text;
        $('loading-bar').style.width = ratio == null ? '100%' : Math.round(ratio * 100) + '%';
      }
      function hideLoading() { $('loading').classList.remove('show'); }

      async function loadSource(url, mimeHint) {
        if (S.abort) S.abort.abort();
        S.abort = null;
        video.pause();
        video.removeAttribute('src');
        video.load();
        releaseBlob();
        S.currentUrl = url;
        if (!S.fullLoad) { video.src = url; return; }

        const ac = new AbortController();
        S.abort = ac;
        showLoading(0, '接続中...');
        try {
          const r = await fetch(url, { signal: ac.signal });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const total = parseInt(r.headers.get('Content-Length')) || 0;
          const type = r.headers.get('Content-Type') || mimeHint || 'video/mp4';
          const reader = r.body.getReader();
          const chunks = [];
          let got = 0;
          const t0 = performance.now();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
            got += value.length;
            const mbps = (got / 1048576) / Math.max((performance.now() - t0) / 1000, 0.1);
            const pct = total ? ` (${Math.round(got * 100 / total)}%)` : '';
            showLoading(total ? got / total : null,
              `全体を読み込み中: ${(got / 1048576).toFixed(1)}MB${total ? ' / ' + (total / 1048576).toFixed(1) + 'MB' : ''}${pct}  ${mbps.toFixed(1)}MB/s`);
          }
          const blob = new Blob(chunks, { type });
          S.blobUrl = URL.createObjectURL(blob);
          video.src = S.blobUrl;
          S.abort = null;
          hideLoading();
          video.play().catch(() => {});
        } catch (e) {
          if (e.name === 'AbortError') return;
          hideLoading();
          toast('全読み込みに失敗したためストリーミングで再生します: ' + e.message);
          video.src = url;
        }
      }

      $('btn-stream').addEventListener('click', () => {
        if (S.abort) S.abort.abort();
        S.abort = null;
        hideLoading();
        if (S.currentUrl) { video.src = S.currentUrl; video.play().catch(() => {}); }
      });

      // ===================== プレーヤー =====================
      function buildSpeedOptions() {
        const speeds = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4];
        $('speed').innerHTML = speeds.map((s) => `<option value="${s}">${s}x</option>`).join('');
      }
      function applySavedPlayerState() {
        const v = parseFloat(localStorage.getItem('volume'));
        video.volume = isNaN(v) ? 1 : v;
        $('vol').value = video.volume;
        const sp = parseFloat(localStorage.getItem('speed')) || 1;
        $('speed').value = sp;
        video.playbackRate = sp;
        $('skip-hint').textContent = `ダブルタップ: ±${S.skip}秒`;
      }

      function skip(sec) {
        if (!isFinite(video.duration)) return;
        video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + sec));
      }
      function togglePlay() { if (video.paused) video.play().catch(() => {}); else video.pause(); }
      function toggleFullscreen() {
        const w = $('player-wrap');
        if (document.fullscreenElement || document.webkitFullscreenElement) {
          (document.exitFullscreen || document.webkitExitFullscreen).call(document);
        } else if (w.requestFullscreen) { w.requestFullscreen(); }
        else if (w.webkitRequestFullscreen) { w.webkitRequestFullscreen(); }
        else if (video.webkitEnterFullscreen) { video.webkitEnterFullscreen(); }
      }
      function flashFx(side, text) {
        const el = $(side === 'L' ? 'fx-left' : 'fx-right');
        el.textContent = text;
        el.classList.add('show');
        clearTimeout(el._t);
        el._t = setTimeout(() => el.classList.remove('show'), 450);
      }

      // タップ / クリック判定 (シングル=再生停止、ダブル=スキップ or 全画面)
      let lastTap = { t: 0, side: '' };
      let tapTimer = null;
      $('tap-layer').addEventListener('pointerup', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        const r = $('tap-layer').getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width;
        const side = x < 0.4 ? 'L' : (x > 0.6 ? 'R' : 'C');
        const now = Date.now();
        if (now - lastTap.t < 320 && lastTap.side === side) {
          clearTimeout(tapTimer);
          if (side === 'L') { skip(-S.skip); flashFx('L', `⏪ ${S.skip}秒`); }
          else if (side === 'R') { skip(S.skip); flashFx('R', `${S.skip}秒 ⏩`); }
          else { toggleFullscreen(); lastTap = { t: 0, side: '' }; return; }
          lastTap = { t: now, side };
        } else {
          lastTap = { t: now, side };
          clearTimeout(tapTimer);
          tapTimer = setTimeout(togglePlay, 320);
        }
      });

      $('btn-play').addEventListener('click', togglePlay);
      $('btn-back').addEventListener('click', () => { skip(-S.skip); flashFx('L', `⏪ ${S.skip}秒`); });
      $('btn-fwd').addEventListener('click', () => { skip(S.skip); flashFx('R', `${S.skip}秒 ⏩`); });
      $('btn-fs').addEventListener('click', toggleFullscreen);
      $('btn-pip').addEventListener('click', async () => {
        try {
          if (document.pictureInPictureElement) await document.exitPictureInPicture();
          else await video.requestPictureInPicture();
        } catch (e) { toast('PiPに対応していません'); }
      });
      $('btn-loop').addEventListener('click', () => {
        video.loop = !video.loop;
        $('btn-loop').textContent = '🔁 ' + (video.loop ? 'ON' : 'OFF');
      });
      $('btn-mute').addEventListener('click', () => { video.muted = !video.muted; });
      $('vol').addEventListener('input', (e) => {
        video.volume = parseFloat(e.target.value);
        video.muted = false;
        localStorage.setItem('volume', e.target.value);
      });
      $('speed').addEventListener('change', (e) => {
        video.playbackRate = parseFloat(e.target.value);
        localStorage.setItem('speed', e.target.value);
      });

      let seeking = false;
      $('seek').addEventListener('input', () => {
        seeking = true;
        if (isFinite(video.duration)) video.currentTime = (parseInt($('seek').value) / 1000) * video.duration;
      });
      $('seek').addEventListener('change', () => { seeking = false; });

      let lastSaved = 0;
      video.addEventListener('timeupdate', () => {
        const d = video.duration;
        $('time').textContent = `${fmt(video.currentTime)} / ${fmt(d)}`;
        if (!seeking && isFinite(d) && d > 0) $('seek').value = Math.round(video.currentTime / d * 1000);
        if (S.current && S.current.id && Date.now() - lastSaved > 3000) {
          lastSaved = Date.now();
          localStorage.setItem('pos:' + S.current.id, video.currentTime);
        }
      });
      video.addEventListener('play', () => { $('btn-play').textContent = '⏸'; });
      video.addEventListener('pause', () => { $('btn-play').textContent = '▶'; });
      video.addEventListener('volumechange', () => { $('btn-mute').textContent = (video.muted || video.volume === 0) ? '🔇' : '🔊'; });
      video.addEventListener('ratechange', () => { $('speed').value = video.playbackRate; });
      video.addEventListener('loadedmetadata', () => {
        video.playbackRate = parseFloat($('speed').value) || 1;
        if (S.current && S.current.id) {
          const pos = parseFloat(localStorage.getItem('pos:' + S.current.id)) || 0;
          if (pos > 5 && pos < video.duration - 5) video.currentTime = pos;
        }
      });
      video.addEventListener('ended', () => {
        if (S.current && S.current.id) localStorage.removeItem('pos:' + S.current.id);
      });
      video.addEventListener('error', () => {
        if (video.getAttribute('src')) toast('動画を再生できませんでした（形式未対応の可能性）');
      });

      document.addEventListener('keydown', (e) => {
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
        if (e.key === ' ' || e.key === 'k') { e.preventDefault(); togglePlay(); }
        else if (e.key === 'ArrowLeft') { skip(-S.skip); flashFx('L', `⏪ ${S.skip}秒`); }
        else if (e.key === 'ArrowRight') { skip(S.skip); flashFx('R', `${S.skip}秒 ⏩`); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); video.volume = Math.min(1, video.volume + 0.05); $('vol').value = video.volume; }
        else if (e.key === 'ArrowDown') { e.preventDefault(); video.volume = Math.max(0, video.volume - 0.05); $('vol').value = video.volume; }
        else if (e.key === 'm') { video.muted = !video.muted; }
        else if (e.key === 'f') { toggleFullscreen(); }
        else if (e.key === '.' || e.key === '>') { stepSpeed(1); }
        else if (e.key === ',' || e.key === '<') { stepSpeed(-1); }
      });
      function stepSpeed(d) {
        const sel = $('speed');
        const i = Math.max(0, Math.min(sel.options.length - 1, sel.selectedIndex + d));
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event('change'));
      }

      // ===================== メモ =====================
      async function saveMemo(item) {
        try {
          $('memo-status').textContent = '保存中...';
          await api('/api/items/' + item.id, { method: 'PATCH', body: { memo: item.memo } });
          $('memo-status').textContent = '✅ 保存済み';
          renderList();
        } catch (e) {
          $('memo-status').textContent = '⚠️ 保存失敗';
          toast(e.message);
        }
      }
      $('memo').addEventListener('input', () => {
        const it = S.current;
        if (!it || it.temp) return;
        it.memo = $('memo').value;
        $('memo-status').textContent = '保存待ち...';
        clearTimeout(memoTimer);
        memoTimer = setTimeout(() => { memoTimer = null; saveMemo(it); }, 800);
        renderChips();
      });
      $('btn-stamp').addEventListener('click', () => {
        const ta = $('memo');
        if (ta.disabled) return;
        const stamp = `[${fmt(video.currentTime)}] `;
        const p = ta.selectionStart ?? ta.value.length;
        ta.value = ta.value.slice(0, p) + stamp + ta.value.slice(p);
        ta.selectionStart = ta.selectionEnd = p + stamp.length;
        ta.focus();
        ta.dispatchEvent(new Event('input'));
      });
      function renderChips() {
        const text = $('memo').value || '';
        const re = /\[(?:(\d+):)?(\d{1,2}):(\d{2})\]/g;
        const out = [];
        let m;
        while ((m = re.exec(text))) {
          const sec = (parseInt(m[1] || 0) * 3600) + parseInt(m[2]) * 60 + parseInt(m[3]);
          out.push(`<button class="chip" data-t="${sec}">▶ ${esc(m[0].slice(1, -1))}</button>`);
        }
        $('chips').innerHTML = out.join('');
      }
      $('chips').addEventListener('click', (e) => {
        const b = e.target.closest('.chip');
        if (!b) return;
        video.currentTime = parseFloat(b.dataset.t);
        video.play().catch(() => {});
      });

      // ===================== 編集ダイアログ =====================
      let editId = null;
      function openEdit(id) {
        const it = S.items.find((i) => i.id === id);
        if (!it) return;
        editId = id;
        $('edit-name').value = it.name;
        $('edit-folder').innerHTML = folderOptionsHtml(it.folder);
        $('edit-info').textContent = `${fmtSize(it.size)} ・ ${it.source || '取り込み'}`;
        $('dlg-edit').showModal();
      }
      $('edit-close').addEventListener('click', () => $('dlg-edit').close());
      $('edit-save').addEventListener('click', async () => {
        try {
          await api('/api/items/' + editId, { method: 'PATCH', body: { name: $('edit-name').value, folder: $('edit-folder').value } });
          $('dlg-edit').close();
          await loadLibrary();
          if (S.current && S.current.id === editId) setMeta(S.current);
        } catch (e) { toast(e.message); }
      });
      $('edit-delete').addEventListener('click', async () => {
        const it = S.items.find((i) => i.id === editId);
        if (!it || !confirm(`「${it.name}」を削除しますか？（Driveのゴミ箱に移動します）`)) return;
        try {
          await api('/api/items/' + editId, { method: 'DELETE' });
          if (S.current && S.current.id === editId) {
            S.current = null;
            video.pause();
            video.removeAttribute('src');
            video.load();
            releaseBlob();
            $('cur-title').textContent = '動画を選択してください';
            $('memo').value = '';
            $('memo').disabled = true;
          }
          $('dlg-edit').close();
          await loadLibrary();
        } catch (e) { toast(e.message); }
      });

      // ===================== 追加ダイアログ =====================
      let currentES = null;
      function openAdd(url) {
        $('add-folder').innerHTML = folderOptionsHtml(S.filter !== '__all__' && S.filter !== '__none__' ? S.filter : '');
        if (url !== undefined) $('add-url').value = url;
        $('add-progress').style.display = 'none';
        $('dlg-add').showModal();
      }
      $('btn-add').addEventListener('click', () => openAdd());
      $('add-close').addEventListener('click', () => { if (currentES) currentES.close(); $('dlg-add').close(); });
      $('btn-save-temp').addEventListener('click', () => {
        if (S.current && S.current.temp) {
          openAdd(S.current.source || '');
          $('add-name').value = S.current.name === '保存前の動画' ? '' : S.current.name;
        }
      });

      $('add-save').addEventListener('click', () => {
        const url = $('add-url').value.trim();
        if (!url) return toast('URLを入力してください');
        const box = $('add-progress');
        box.style.display = 'block';
        box.textContent = '🚀 サーバーへ要求を送信中...';
        if (currentES) currentES.close();
        const q = new URLSearchParams({
          url, name: $('add-name').value.trim(), mode: $('add-mode').value, folder: $('add-folder').value
        });
        const es = new EventSource('/api/save?' + q.toString());
        currentES = es;
        es.onmessage = async (ev) => {
          const d = JSON.parse(ev.data);
          if (d.type === 'progress') box.textContent = d.message;
          else if (d.type === 'success') {
            es.close();
            box.textContent = '✅ 保存しました: ' + d.item.name;
            $('add-url').value = '';
            $('add-name').value = '';
            await loadLibrary();
            $('dlg-add').close();
            openItem(d.item.id);
          } else if (d.type === 'error') {
            es.close();
            box.textContent = '❌ ' + d.message;
          }
        };
        es.onerror = () => { box.textContent += '\n⚠️ 通信が切断されました'; es.close(); };
      });

      $('add-preview').addEventListener('click', async () => {
        const url = $('add-url').value.trim();
        if (!url) return toast('URLを入力してください');
        const box = $('add-progress');
        box.style.display = 'block';
        box.textContent = '🕵️ 動画URLを抽出中... (数十秒かかる場合があります)';
        try {
          const j = await api('/api/extract?url=' + encodeURIComponent(url));
          $('dlg-add').close();
          await openTemp($('add-name').value.trim() || j.title || '保存前の動画', j.proxy, url);
        } catch (e) { box.textContent = '❌ ' + e.message; }
      });

      // ===================== 設定ダイアログ =====================
      function openSettings() {
        $('set-drive').textContent = S.cfg.drive_ready ? '✅ 接続済み' : (S.cfg.client_configured ? '⚠️ 未接続（認証してください）' : '⚠️ GOOGLE_CLIENT_ID / SECRET が未設定');
        $('set-root').value = S.cfg.root_path || localStorage.getItem('rootPath') || '';
        $('set-skip').value = S.skip;
        $('set-full').checked = S.fullLoad;
        if (!$('dlg-settings').open) $('dlg-settings').showModal();
      }
      $('btn-settings').addEventListener('click', openSettings);
      $('set-close').addEventListener('click', () => $('dlg-settings').close());
      $('set-root-save').addEventListener('click', async () => {
        try {
          const path = $('set-root').value.trim();
          S.cfg = await api('/api/config', { method: 'POST', body: { root_path: path } });
          localStorage.setItem('rootPath', S.cfg.root_path);
          updateStatus();
          await loadLibrary();
          toast('ルートディレクトリを適用しました');
        } catch (e) { toast(e.message); }
      });
      $('set-skip').addEventListener('change', (e) => {
        S.skip = Math.max(1, parseInt(e.target.value) || 10);
        localStorage.setItem('skipSec', S.skip);
        $('skip-hint').textContent = `ダブルタップ: ±${S.skip}秒`;
      });
      $('set-full').addEventListener('change', (e) => {
        S.fullLoad = e.target.checked;
        localStorage.setItem('fullLoad', S.fullLoad ? '1' : '0');
      });
      $('set-scan').addEventListener('click', async () => {
        try {
          const j = await api('/api/scan', { method: 'POST' });
          toast(`${j.added}件を取り込みました`);
          await loadLibrary();
        } catch (e) { toast(e.message); }
      });
      $('set-reload').addEventListener('click', async () => {
        try { await api('/api/library/reload', { method: 'POST' }); await loadLibrary(); toast('再読込しました'); }
        catch (e) { toast(e.message); }
      });

      init();
    </script>
  </body>
</html>
"""


@app.route('/')
async def index():
    return Response(HTML_TEMPLATE, content_type='text/html; charset=utf-8')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

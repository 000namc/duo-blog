#!/usr/bin/env python3
"""duo-blog 서버 (표준 라이브러리만).

tailnet 전용으로 뜨는 단일 프로세스:
- fuwari 빌드 결과(dist/)를 로그인 뒤에만 서빙
- /write 글 목록·작성·수정 + 저장 시 pnpm 재빌드
- /login /logout /account(비밀번호 변경) — 자체 계정(users.json, scrypt)
- 가입 페이지는 없다. 계정은 관리자가 CLI로 만든다:
    python3 server.py adduser <이메일> <표시이름> [비밀번호]
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
DIST = os.path.join(REPO, "dist")
POSTS = os.path.join(REPO, "src", "content", "posts")
USERS_FILE = os.path.join(ROOT, "users.json")
SECRET_FILE = os.path.join(ROOT, "secret.key")
BUILD_LOG = os.path.join(ROOT, "build.log")
PORT = int(os.environ.get("DUO_BLOG_PORT", "4600"))
SESSION_SECONDS = 90 * 24 * 3600
NODE_BIN = os.environ.get(
	"DUO_BLOG_NODE_BIN", os.path.expanduser("~/.local/node/bin")
)

MIME = {
	".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
	".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
	".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
	".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2",
	".woff": "font/woff", ".txt": "text/plain; charset=utf-8", ".xml": "application/xml",
	".webmanifest": "application/manifest+json", ".pf_meta": "application/octet-stream",
	".pf_index": "application/octet-stream", ".pf_fragment": "application/octet-stream",
	".pagefind": "application/octet-stream", ".wasm": "application/wasm",
	".mp4": "video/mp4", ".pdf": "application/pdf",
}

# ---------- 계정 ----------

def load_users():
	if not os.path.exists(USERS_FILE):
		return {}
	with open(USERS_FILE) as f:
		return json.load(f)


def save_users(users):
	tmp = USERS_FILE + ".tmp"
	with open(tmp, "w") as f:
		json.dump(users, f, ensure_ascii=False, indent=1)
	os.replace(tmp, USERS_FILE)


def hash_password(password, salt=None):
	# scrypt는 macOS 시스템 파이썬(LibreSSL)에 없어서 PBKDF2 사용
	salt = salt or secrets.token_bytes(16)
	digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
	return salt.hex(), digest.hex()


def verify_password(users, email, password):
	u = users.get(email)
	if not u:
		return False
	_, digest = hash_password(password, bytes.fromhex(u["salt"]))
	return hmac.compare_digest(digest, u["hash"])


def set_password(email, name, password):
	users = load_users()
	salt, digest = hash_password(password)
	users[email] = {"salt": salt, "hash": digest, "name": name or users.get(email, {}).get("name", email)}
	save_users(users)


# ---------- 세션 (서명 쿠키) ----------

def secret_key():
	if not os.path.exists(SECRET_FILE):
		with open(SECRET_FILE, "w") as f:
			f.write(secrets.token_hex(32))
		os.chmod(SECRET_FILE, 0o600)
	with open(SECRET_FILE) as f:
		return f.read().strip().encode()


SECRET = None  # 서버 기동 시 채움


def make_token(email):
	exp = str(int(time.time()) + SESSION_SECONDS)
	body = f"{email.encode().hex()}.{exp}"
	sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
	return f"{body}.{sig}"


def check_token(token):
	try:
		email_hex, exp, sig = token.split(".")
		body = f"{email_hex}.{exp}"
		want = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
		if not hmac.compare_digest(sig, want) or int(exp) < time.time():
			return None
		email = bytes.fromhex(email_hex).decode()
		return email if email in load_users() else None
	except Exception:
		return None


# ---------- 재빌드 ----------

build_state = {"running": False, "ok": None, "finished": None, "message": "아직 빌드한 적 없음"}
build_lock = threading.Lock()


def rebuild_async(reason):
	def run():
		env = dict(os.environ, PATH=NODE_BIN + os.pathsep + os.environ.get("PATH", ""))
		with open(BUILD_LOG, "a") as log:
			log.write(f"\n=== {time.strftime('%F %T')} {reason}\n")
			log.flush()
			r = subprocess.run(
				["pnpm", "build"], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT
			)
		with build_lock:
			build_state.update(
				running=False, ok=(r.returncode == 0), finished=time.strftime("%H:%M:%S"),
				message="반영 완료" if r.returncode == 0 else "빌드 실패 — build.log 확인",
			)
		subprocess.run(["git", "add", "-A", "src/content", "public"], cwd=REPO, capture_output=True)
		c = subprocess.run(["git", "commit", "-m", reason], cwd=REPO, capture_output=True)
		if c.returncode == 0:
			subprocess.run(["git", "push", "origin", "main"], cwd=REPO, capture_output=True)

	with build_lock:
		if build_state["running"]:
			return
		build_state.update(running=True, message="빌드 중…")
	threading.Thread(target=run, daemon=True).start()


# ---------- HTML ----------

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;max-width:680px;margin:0 auto;
padding:24px 16px;background:#faf6f0;color:#333}}
h1{{font-size:1.3em}} a{{color:#a5673f}} .card{{background:#fff;border-radius:12px;padding:16px;
margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
input,textarea{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0 12px;border:1px solid #ddd;
border-radius:8px;font-size:16px;font-family:inherit}}
textarea{{min-height:320px;font-family:ui-monospace,monospace}}
button{{background:#a5673f;color:#fff;border:0;border-radius:8px;padding:10px 20px;font-size:16px}}
.muted{{color:#888;font-size:.85em}} nav{{margin-bottom:8px}} nav a{{margin-right:12px}}
.err{{color:#c0392b}}</style></head><body>{nav}{body}</body></html>"""


def page(title, body, nav=True):
	navigation = (
		'<nav><a href="/">🍪 블로그</a><a href="/write">글 목록</a>'
		'<a href="/account">계정</a><a href="/logout">로그아웃</a></nav>'
		if nav else ""
	)
	return PAGE.format(title=html.escape(title), nav=navigation, body=body).encode()


def dist_stylesheets():
	"""빌드 결과물에서 스타일시트 링크를 뽑아 미리보기에 그대로 입힌다."""
	src = None
	posts_dir = os.path.join(DIST, "posts")
	if os.path.isdir(posts_dir):
		for d in sorted(os.listdir(posts_dir)):
			p = os.path.join(posts_dir, d, "index.html")
			if os.path.exists(p):
				src = p
				break
	if not src:
		src = os.path.join(DIST, "index.html")
	if not os.path.exists(src):
		return ""
	with open(src) as f:
		return "\n".join(re.findall(r'<link rel="stylesheet"[^>]*>', f.read()))


EDITOR_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{styles}
<style>
body{{margin:0;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;background:#faf6f0}}
.ed-top{{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#fff;
border-bottom:1px solid #e5ddd2;position:sticky;top:0;z-index:10;flex-wrap:wrap}}
.ed-top .name{{font-size:.85em;color:#888;flex:1;min-width:120px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}}
.ed-top a{{color:#a5673f;text-decoration:none;font-size:.9em}}
.ed-btn{{background:#a5673f;color:#fff;border:0;border-radius:8px;padding:8px 18px;font-size:15px}}
.ed-btn.gray{{background:#999}} .ed-btn.red{{background:#c0392b}}
.ed-main{{display:flex;height:calc(100dvh - 49px)}}
#src{{flex:1;min-width:0;border:0;padding:16px;font:14px/1.7 ui-monospace,Menlo,monospace;
resize:none;outline:none;background:#fff}}
#pv{{flex:1;min-width:0;border-left:1px solid #e5ddd2;overflow-y:auto;padding:24px 28px;background:#fff}}
#pv h1.pv-title{{font-size:1.6em;margin:0 0 .8em}}
.meta{{display:none;padding:10px 12px;background:#fff;border-bottom:1px solid #e5ddd2}}
.meta.open{{display:block}}
.meta input{{width:100%;box-sizing:border-box;padding:8px;margin:4px 0;border:1px solid #ddd;
border-radius:6px;font-size:15px}}
#pvbtn{{display:none}}
@media (max-width:800px){{
  #pvbtn{{display:inline-block}}
  body.showpv #src{{display:none}}
  body:not(.showpv) #pv{{display:none}}
  #pv{{border-left:0}}
}}
</style></head><body>
<form method="post" action="{action}" id="f">
<div class="ed-top">
  <a href="{back}">←</a><span class="name">{name}</span>
  {metabtn}
  <button type="button" class="ed-btn gray" id="pvbtn">미리보기</button>
  <button class="ed-btn">저장</button>
</div>
{meta}
<div class="ed-main">
<textarea id="src" name="{field}" placeholder="마크다운으로 쓰면 오른쪽에 블로그 모양 그대로 보여요">{content}</textarea>
<div id="pv"><h1 class="pv-title" id="pvt"></h1>
<div id="pvc" class="prose dark:prose-invert prose-base !max-w-none custom-md markdown-content"></div></div>
</div>
{hidden}
</form>
{delform}
<script src="/server-assets/marked.min.js"></script>
<script>
const src=document.getElementById('src'),pvt=document.getElementById('pvt'),
      pvc=document.getElementById('pvc'),titleInput=document.querySelector('input[name=title]');
function split(text){{
  const m=text.match(/^---\\n([\\s\\S]*?)\\n---\\n?/);
  if(!m) return {{fm:null,body:text}};
  return {{fm:m[1],body:text.slice(m[0].length)}};
}}
function render(){{
  const {{fm,body}}=split(src.value);
  let title='';
  if(titleInput) title=titleInput.value;
  else if(fm){{const t=fm.match(/^title:\\s*(.*)$/m); if(t) title=t[1].replace(/^["']|["']$/g,'');}}
  pvt.textContent=title;
  pvc.innerHTML=marked.parse(body);
}}
let timer=null;
function queue(){{clearTimeout(timer);timer=setTimeout(render,250);}}
src.addEventListener('input',queue);
if(titleInput) titleInput.addEventListener('input',queue);
document.getElementById('pvbtn').addEventListener('click',()=>{{
  document.body.classList.toggle('showpv');render();
}});
const mb=document.getElementById('metabtn');
if(mb) mb.addEventListener('click',()=>document.querySelector('.meta').classList.toggle('open'));
render();
</script></body></html>"""


def editor_html(title, action, field, content, name, back="/", hidden="", meta="", metabtn="", delform=""):
	return EDITOR_PAGE.format(
		title=html.escape(title), styles=dist_stylesheets(), action=action, field=field,
		content=html.escape(content), name=html.escape(name), back=back, hidden=hidden,
		meta=meta, metabtn=metabtn, delform=delform,
	).encode()


# 블로그 화면에 띄우는 플로팅 편집 버튼 (티스토리식 연필 아이콘)
def toolbar_html(post_name=None):
	buttons = ""
	if post_name:
		buttons += (
			f'<a href="/write/edit?f={urllib.parse.quote(post_name)}" title="이 글 수정" '
			'style="background:#a5673f">✏️</a>'
		)
	buttons += '<a href="/write/new" title="새 글" style="background:#5a8a5a">＋</a>'
	return (
		'<div style="position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;'
		'gap:10px;z-index:9999">'
		+ buttons.replace(
			'<a ',
			'<a class="ch-fab" ',
		)
		+ '</div><style>.ch-fab{width:48px;height:48px;border-radius:50%;display:flex;'
		'align-items:center;justify-content:center;color:#fff;font-size:22px;'
		'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.25)}</style>'
	).encode()


def post_name_for_path(path):
	m = re.match(r"^/posts/([^/]+)/?$", urllib.parse.unquote(path))
	if not m:
		return None
	name = m.group(1) + ".md"
	return name if safe_post_name(name) and os.path.exists(os.path.join(POSTS, name)) else None


def post_url_for_name(name):
	return "/posts/" + urllib.parse.quote(name[:-3]) + "/"


# ---------- 핸들러 ----------

class Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"

	def log_message(self, fmt, *args):
		sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

	# 응답 헬퍼
	def send_page(self, data, status=200, ctype="text/html; charset=utf-8", extra=None):
		self.send_response(status)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store" if ctype.startswith("text/html") else "max-age=3600")
		for k, v in (extra or []):
			self.send_header(k, v)
		self.end_headers()
		self.wfile.write(data)

	def redirect(self, to, cookie=None):
		self.send_response(303)
		self.send_header("Location", to)
		if cookie:
			self.send_header("Set-Cookie", cookie)
		self.send_header("Content-Length", "0")
		self.end_headers()

	def current_user(self):
		c = self.headers.get("Cookie", "")
		m = re.search(r"chs=([^;\s]+)", c)
		return check_token(m.group(1)) if m else None

	def form(self):
		length = int(self.headers.get("Content-Length", 0))
		raw = self.rfile.read(length).decode()
		return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

	# GET
	def do_GET(self):
		path = urllib.parse.urlsplit(self.path).path
		if path == "/health":
			return self.send_page(b"ok", ctype="text/plain")
		if path == "/build/status":
			with build_lock:
				data = json.dumps(build_state).encode()
			return self.send_page(data, ctype="application/json")
		if path == "/login":
			return self.send_page(page("로그인", LOGIN_FORM, nav=False))
		user = self.current_user()
		if not user:
			return self.redirect("/login")
		if path == "/logout":
			return self.redirect("/login", cookie="chs=; Max-Age=0; Path=/")
		if path == "/account":
			return self.send_page(page("계정", account_body(user)))
		if path == "/server-assets/marked.min.js":
			with open(os.path.join(ROOT, "marked.min.js"), "rb") as f:
				return self.send_page(f.read(), ctype="text/javascript")
		if path == "/write":
			return self.send_page(page("글쓰기", write_list_body()))
		if path == "/write/new":
			meta = """<div class="meta open">
<input name="title" placeholder="제목" required autofocus>
<input name="description" placeholder="한 줄 설명 (선택)">
<input name="category" placeholder="카테고리 (선택)">
<input name="tags" placeholder="태그 — 쉼표로 구분 (선택)">
<label style="font-size:.85em;color:#888"><input type="checkbox" name="draft" value="1" style="width:auto"> 초안 (블로그에 숨김)</label>
</div>"""
			metabtn = '<button type="button" class="ed-btn gray" id="metabtn">정보</button>'
			return self.send_page(
				editor_html("새 글", "/write/create", "body", "", "새 글", back="/", meta=meta, metabtn=metabtn)
			)
		if path == "/write/edit":
			q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
			return self.edit_page(q.get("f", [""])[0])
		if path == "/write/wait":
			q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
			to = q.get("to", ["/"])[0]
			if not to.startswith("/"):
				to = "/"
			body = f"""<div class="card" style="margin-top:20vh;text-align:center">
<h1>반영하는 중…</h1><p class="muted" id="s">빌드가 끝나면 자동으로 이동해요.</p></div>
<script>
const to={json.dumps(to)};
async function poll(){{
  try{{
    const r=await fetch('/build/status');const s=await r.json();
    if(!s.running){{
      if(s.ok===false){{document.getElementById('s').textContent='빌드 실패 — build.log를 확인해주세요.';return;}}
      location.href=to;return;
    }}
  }}catch(e){{}}
  setTimeout(poll,2000);
}}
setTimeout(poll,2000);
</script>"""
			return self.send_page(page("반영 중", body))
		return self.serve_static(path)

	# POST
	def do_POST(self):
		path = urllib.parse.urlsplit(self.path).path
		if path == "/login":
			f = self.form()
			email = f.get("email", "").strip().lower()
			if verify_password(load_users(), email, f.get("password", "")):
				cookie = (
					f"chs={make_token(email)}; Max-Age={SESSION_SECONDS}; Path=/; "
					"HttpOnly; Secure; SameSite=Lax"
				)
				return self.redirect("/", cookie=cookie)
			body = LOGIN_FORM.replace("<!--err-->", '<p class="err">이메일 또는 비밀번호가 맞지 않아요.</p>')
			return self.send_page(page("로그인", body, nav=False), status=401)
		user = self.current_user()
		if not user:
			return self.redirect("/login")
		if path == "/account":
			f = self.form()
			users = load_users()
			if not verify_password(users, user, f.get("old", "")):
				return self.send_page(page("계정", account_body(user, "현재 비밀번호가 맞지 않아요.")))
			if len(f.get("new1", "")) < 8 or f.get("new1") != f.get("new2"):
				return self.send_page(page("계정", account_body(user, "새 비밀번호가 8자 미만이거나 두 칸이 서로 달라요.")))
			set_password(user, users[user]["name"], f["new1"])
			return self.send_page(page("계정", account_body(user, "비밀번호를 바꿨어요.", ok=True)))
		if path == "/write/create":
			f = self.form()
			name = post_filename(f.get("title", ""))
			write_post_file(name, compose_post(f))
			rebuild_async(f"글 작성: {name} ({load_users()[user]['name']})")
			to = "/write" if f.get("draft") else post_url_for_name(name)
			return self.redirect("/write/wait?to=" + urllib.parse.quote(to))
		if path == "/write/save":
			f = self.form()
			name = safe_post_name(f.get("f", ""))
			if not name:
				return self.send_page(page("오류", "<p>잘못된 파일 이름.</p>"), status=400)
			write_post_file(name, f.get("content", ""))
			rebuild_async(f"글 수정: {name} ({load_users()[user]['name']})")
			return self.redirect("/write/wait?to=" + urllib.parse.quote(post_url_for_name(name)))
		if path == "/write/delete":
			f = self.form()
			name = safe_post_name(f.get("f", ""))
			if name and os.path.exists(os.path.join(POSTS, name)):
				os.remove(os.path.join(POSTS, name))
				rebuild_async(f"글 삭제: {name} ({load_users()[user]['name']})")
			return self.redirect("/write/wait?to=%2F")
		self.send_page(page("404", "<p>없는 주소예요.</p>"), status=404)

	# 정적 서빙 (dist/)
	def serve_static(self, path):
		rel = urllib.parse.unquote(path).lstrip("/")
		full = os.path.realpath(os.path.join(DIST, rel))
		if not full.startswith(os.path.realpath(DIST)):
			return self.send_page(b"forbidden", status=403, ctype="text/plain")
		if os.path.isdir(full):
			full = os.path.join(full, "index.html")
		if not os.path.exists(full) and not os.path.splitext(full)[1]:
			alt = full + ".html"
			full = alt if os.path.exists(alt) else full
		if not os.path.exists(full):
			nf = os.path.join(DIST, "404.html")
			if os.path.exists(nf):
				with open(nf, "rb") as f:
					return self.send_page(f.read(), status=404)
			return self.send_page(page("404", "<p>없는 페이지예요.</p>"), status=404)
		ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
		with open(full, "rb") as f:
			data = f.read()
		if ctype.startswith("text/html"):
			data = data.replace(
				b"</body>", toolbar_html(post_name_for_path(path)) + b"</body>", 1
			)
		self.send_page(data, ctype=ctype)

	def edit_page(self, name):
		name = safe_post_name(name)
		fp = os.path.join(POSTS, name) if name else None
		if not fp or not os.path.exists(fp):
			return self.send_page(page("오류", "<p>글을 찾을 수 없어요.</p>"), status=404)
		with open(fp) as f:
			content = f.read()
		hidden = f'<input type="hidden" name="f" value="{html.escape(name)}">'
		delform = (
			f'<form method="post" action="/write/delete" id="del" style="display:none">'
			f'<input type="hidden" name="f" value="{html.escape(name)}"></form>'
		)
		metabtn = (
			'<button type="button" class="ed-btn red" '
			"onclick=\"if(confirm('정말 삭제할까요?'))document.getElementById('del').submit()\">삭제</button>"
		)
		self.send_page(
			editor_html(
				f"수정 — {name}", "/write/save", "content", content, name,
				back=post_url_for_name(name), hidden=hidden, metabtn=metabtn, delform=delform,
			)
		)


LOGIN_FORM = """<div class="card" style="margin-top:15vh"><h1>✌️ Duo Blog</h1>
<!--err--><form method="post" action="/login">
<input name="email" type="email" placeholder="이메일" autocomplete="username" required>
<input name="password" type="password" placeholder="비밀번호" autocomplete="current-password" required>
<button>들어가기</button></form></div>"""

def account_body(email, msg=None, ok=False):
	users = load_users()
	name = html.escape(users[email]["name"])
	note = f'<p class="{"muted" if ok else "err"}">{html.escape(msg)}</p>' if msg else ""
	return f"""<h1>계정</h1><div class="card"><p>{name} &lt;{html.escape(email)}&gt;</p>{note}
<form method="post" action="/account">
<input name="old" type="password" placeholder="현재 비밀번호" required>
<input name="new1" type="password" placeholder="새 비밀번호 (8자 이상)" required>
<input name="new2" type="password" placeholder="새 비밀번호 확인" required>
<button>비밀번호 변경</button></form></div>"""


def write_list_body():
	items = []
	for fn in sorted(os.listdir(POSTS), key=lambda n: os.path.getmtime(os.path.join(POSTS, n)), reverse=True):
		if fn.endswith(".md"):
			items.append(
				f'<li><a href="/write/edit?f={urllib.parse.quote(fn)}">{html.escape(fn)}</a></li>'
			)
	with build_lock:
		status = build_state["message"]
		if build_state["finished"] and not build_state["running"]:
			status += f' ({build_state["finished"]})'
	return (
		f'<h1>글쓰기</h1><p class="muted">빌드 상태: {html.escape(status)}</p>'
		f'<div class="card"><a href="/write/new"><button>새 글 쓰기</button></a></div>'
		f'<div class="card"><ul>{"".join(items) or "<li>아직 글이 없어요</li>"}</ul></div>'
	)


def safe_post_name(name):
	name = os.path.basename(name.strip())
	return name if re.fullmatch(r"[\w.\-가-힣 ]+\.md", name) else None


def post_filename(title):
	slug = re.sub(r"[^a-z0-9가-힣]+", "-", title.lower()).strip("-") or "글"
	base = time.strftime("%Y%m%d") + "-" + slug
	name, n = base + ".md", 2
	while os.path.exists(os.path.join(POSTS, name)):
		name, n = f"{base}-{n}.md", n + 1
	return name


def compose_post(f):
	tags = [t.strip() for t in f.get("tags", "").split(",") if t.strip()]
	tags_yaml = "[" + ", ".join(tags) + "]"
	lines = [
		"---",
		f"title: {json.dumps(f.get('title', ''), ensure_ascii=False)}",
		f"published: {time.strftime('%Y-%m-%d')}",
		f"description: {json.dumps(f.get('description', ''), ensure_ascii=False)}",
		f"tags: {tags_yaml}",
		f"category: {json.dumps(f.get('category', ''), ensure_ascii=False)}",
		f"draft: {'true' if f.get('draft') else 'false'}",
		"---",
		"",
		f.get("body", ""),
		"",
	]
	return "\n".join(lines)


def write_post_file(name, content):
	os.makedirs(POSTS, exist_ok=True)
	with open(os.path.join(POSTS, name), "w") as f:
		f.write(content)


# ---------- CLI ----------

def main():
	global SECRET
	if len(sys.argv) > 1 and sys.argv[1] == "adduser":
		email = sys.argv[2].strip().lower()
		name = sys.argv[3] if len(sys.argv) > 3 else email
		password = sys.argv[4] if len(sys.argv) > 4 else secrets.token_urlsafe(9)
		set_password(email, name, password)
		print(f"계정 저장: {email} ({name})  임시 비밀번호: {password}")
		return
	SECRET = secret_key()
	if not load_users():
		print("경고: users.json이 비어 있음 — adduser로 계정을 먼저 만들 것", file=sys.stderr)
	server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
	print(f"duo-blog server on http://127.0.0.1:{PORT}  (repo={REPO})")
	server.serve_forever()


if __name__ == "__main__":
	main()

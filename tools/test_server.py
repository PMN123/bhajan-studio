"""Server-mode checks: the real server.py is started (as `python server.py`, the way the launchers and the Docker
image start it) and asked over HTTP about the health check, the password (BHAJAN_PASSWORD: the sign-in page, its
cookie, and HTTP Basic auth for scripts), the host names it answers to (BHAJAN_ALLOWED_HOSTS), the guard against
other websites, and the file types it serves. Nothing is
fetched from Sai Rhythms and no extra packages are needed, so it runs the same on macOS, Linux and Windows.
Run from the repo:  uv run python tools/test_server.py
"""
import base64, http.client, json, os, re, subprocess, sys, tempfile, threading, time, tomllib

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]
TMP = tempfile.TemporaryDirectory(prefix="bhajan-server-test-")
failed, _mark = [], 0


def check(ok, what, detail=""):
    """Record one check; failures are listed at the end instead of stopping the run."""
    if not ok:
        failed.append(what)
        print(f"FAIL {what}" + (f" ({detail})" if detail else ""))
    return ok


def section(name, detail=""):
    global _mark
    bad, _mark = len(failed) - _mark, len(failed)
    print(f"{name} checks " + (f"OK ({detail})" if not bad else f"FAILED ({bad})"))


class Server:
    """server.py in a child process, with `env` added to the environment; .port is the port it chose."""

    def __init__(self, *args, **env):
        self.out = []
        self.proc = subprocess.Popen([sys.executable, "server.py", "--port", "8791", *args], cwd=ROOT,
                                     env=os.environ | {"BHAJAN_DATA": os.path.join(TMP.name, "data"),
                                                       "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"} | env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace")
        self.port = None
        ready = threading.Event()

        def read():
            for line in self.proc.stdout:
                self.out.append(line)
                m = re.search(r"Open in your browser:\s+http://[^:]+:(\d+)", line)
                if m:
                    self.port = int(m.group(1))
                if "To stop it:" in line:
                    ready.set()
            ready.set()
        threading.Thread(target=read, daemon=True).start()
        if not ready.wait(120) or self.port is None:
            self.stop()
            raise SystemExit("server.py did not start:\n" + "".join(self.out))

    def text(self):
        return "".join(self.out)

    def get(self, path, method="GET", host=None, password=None, headers=None, body=None):
        """(status, headers, body) of one request to 127.0.0.1, optionally with another Host header."""
        h = dict(headers or {})
        if host:
            h["Host"] = host
        if password is not None:
            h["Authorization"] = "Basic " + base64.b64encode(f"anyone:{password}".encode()).decode()
        if body is not None:
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            conn.close()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def ctype(r):
    return r[1].get("content-type", "").split(";")[0]


def is_login(r):
    return r[0] == 200 and b'action="/api/login"' in r[2]


def is_app(r):
    return r[0] == 200 and b"brand-mark" in r[2] and b'action="/api/login"' not in r[2]


# ------------------------------------------------------------------ on this computer only (the default)
s = Server()
try:
    r = s.get("/api/health")
    j = json.loads(r[2]) if r[0] == 200 else {}
    check(r[0] == 200 and j.get("ok") is True and j.get("version") == VERSION, "/api/health answers with the version",
          (r[0], r[2][:200]))
    check(isinstance(j.get("parts"), bool) and j.get("ocr") in ("vision", "tesseract", "none"),
          "/api/health says whether Parts and OCR are available", j)
    check(s.get("/")[0] == 200, "the app opens with no password by default")
    check(s.get("/", host="evil.example")[0] == 403, "requests addressed to another host name are refused")
    check(s.get("/api/search?q=a", headers={"Origin": "https://evil.example"})[0] == 403,
          "other websites may not use the API")
    check(s.get("/api/search?q=a", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403,
          "cross-site requests (Sec-Fetch-Site) are refused")
    check("WARNING" not in s.text(), "no warning when listening on this computer only", s.text())
    types = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css", "/stems.js": "text/javascript"}
    if os.path.exists(os.path.join(ROOT, "static", "manifest.webmanifest")):
        types["/manifest.webmanifest"] = "application/manifest+json"
    for path, want in types.items():
        r = s.get(path)
        check(r[0] == 200 and ctype(r) == want, f"{path} is served as {want}", (r[0], ctype(r)))
finally:
    s.stop()
section("local", "health, host check, other websites refused, file types")

# ------------------------------------------------------------------ on a server, with a password
s = Server("--host", "0.0.0.0", BHAJAN_PASSWORD="correct horse", BHAJAN_ALLOWED_HOSTS="Bhajans.Example.org, other.test:443")
try:
    r = s.get("/")
    check(is_login(r) and "www-authenticate" not in r[1] and r[1].get("cache-control") == "no-store",
          "without the password the home page is the sign-in page (a 200, for hosts that check it)", (r[0], r[2][:80]))
    check(is_login(s.get("/index.html")) and is_login(s.get("/?wrong")), "so are /index.html and /?wrong")
    check(is_login(s.get("/login.html")), "the sign-in page needs no password")

    # signing in
    tm = time.time()
    r = s.get("/api/login", method="POST", body="username=x&password=wrong")
    check(r[0] == 303 and r[1].get("location") == "/?wrong" and "set-cookie" not in r[1], "a wrong password is refused",
          (r[0], r[1].get("location")))
    check(time.time() - tm >= 0.9, "a wrong password costs a second", f"{time.time() - tm:.2f}s")
    r = s.get("/api/login", method="POST", body="username=x&password=correct+horse")
    cookie = r[1].get("set-cookie", "")
    check(r[0] == 303 and r[1].get("location") == "/" and cookie.startswith("bhajan_session=")
          and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower() and "secure" not in cookie.lower(),
          "the right password sets an HttpOnly, SameSite cookie and goes to the app", (r[0], cookie))
    session = cookie.split(";")[0]
    check(is_app(s.get("/", headers={"Cookie": session})), "the cookie opens the app")
    check(s.get("/app.js", headers={"Cookie": session})[0] == 200, "and its files")
    check(s.get("/api/no-such-route", headers={"Cookie": session})[0] == 404, "and the API")
    issued, sig = session.split("=", 1)[1].split(".")
    for bad, what in ((f"{issued}.{'0' * len(sig)}", "a forged cookie"), (f"{int(issued) + 1}.{sig}", "an altered cookie"),
                      ("1.", "a malformed cookie"), ("²³.x", "a cookie with non-ASCII digits")):
        check(is_login(s.get("/", headers={"Cookie": f"bhajan_session={bad}"})), f"{what} does not")
    r = s.get("/api/login", method="POST", body="password=correct+horse", headers={"X-Forwarded-Proto": "https"})
    check("secure" in r[1].get("set-cookie", "").lower(), "behind an HTTPS proxy the cookie is Secure", r[1].get("set-cookie"))
    check(s.get("/api/login", method="POST", body="password=correct+horse",
                headers={"Origin": "https://evil.example"})[0] == 403, "other websites may not sign in")

    # scripts: HTTP Basic auth
    check(is_app(s.get("/", password="correct horse")), "HTTP Basic auth with the password opens the app")
    check(is_login(s.get("/", password="wrong")), "a wrong Basic password does not")
    check(is_login(s.get("/", headers={"Authorization": "Basic !!!"})), "a malformed Authorization header is refused")
    for path in ("/app.js", "/api/search?q=a", "/media/x.mp3", "/stems/x/voice.flac", "/icons/%2e%2e/index.html",
                 "/icons/../index.html", "/login.html/../index.html"):
        r = s.get(path)
        check(r[0] == 401 and b"brand-mark" not in r[2], f"{path} needs the password", r[0])
    check(s.get("/api/health", method="POST")[0] == 401, "only GET /api/health is open")
    r = s.get("/api/health")
    check(r[0] == 200 and json.loads(r[2]).get("ok") is True, "/api/health needs no password")
    check(s.get("/api/health", host="10.0.0.5:8777")[0] == 200, "/api/health answers a load balancer's IP-address Host")
    check(is_app(s.get("/", host="bhajans.example.org", password="correct horse"))
          and is_app(s.get("/", host="other.test", password="correct horse")), "listed host names work (any case, any port)")
    check(is_app(s.get("/", host="localhost:8777", password="correct horse")), "localhost always works")
    check(s.get("/", host="evil.example", password="correct horse")[0] == 403, "unlisted host names are refused")
    check(s.get("/api/search?q=a", host="bhajans.example.org", password="correct horse",
                headers={"Origin": "https://evil.example"})[0] == 403, "other websites may not use the API with the password")
    r = s.get("/api/no-such-route", host="bhajans.example.org", password="correct horse",   # 404: past the guard
              headers={"Origin": "https://bhajans.example.org", "Sec-Fetch-Site": "same-origin"})
    check(r[0] == 404, "the app's own page (behind an HTTPS proxy) may", r[0])
    if os.path.exists(os.path.join(ROOT, "static", "manifest.webmanifest")):
        check(s.get("/manifest.webmanifest")[0] == 200, "the manifest needs no password (browsers fetch it without one)")
    icons = os.path.join(ROOT, "static", "icons")
    for name in (sorted(os.listdir(icons))[:1] if os.path.isdir(icons) else []):
        check(s.get(f"/icons/{name}")[0] == 200, "the app icons need no password")
    check("WARNING" not in s.text() and "password" in s.text(), "the banner says a password is needed", s.text())
finally:
    s.stop()
section("password", "sign-in page, cookie, Basic auth, 401s, health and install files open, host names, "
                    "other websites refused")

# ------------------------------------------------------------------ on a server, without a password
s = Server("--host", "0.0.0.0")
try:
    check("WARNING" in s.text() and "BHAJAN_PASSWORD" in s.text(), "listening publicly without a password warns",
          s.text())
    check(s.get("/", host="192.168.1.20:8777")[0] == 200, "with no BHAJAN_ALLOWED_HOSTS any host name works")
finally:
    s.stop()
section("public", "warning without a password, any host name")

TMP.cleanup()
if failed:
    print(f"{len(failed)} SERVER CHECK(S) FAILED:\n  " + "\n  ".join(failed))
    sys.exit(1)
print("ALL SERVER TESTS PASSED")

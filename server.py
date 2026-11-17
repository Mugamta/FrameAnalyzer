"""FrameAnalyzer 로컬 서버.

- 인스타/유튜브/트위터 등 링크 → yt-dlp로 다운로드
- 드래그앤드롭한 파일 → 로컬로 업로드(이 컴퓨터 밖으로 나가지 않음)
- ffmpeg로 모든 프레임을 디코딩해 프레임 간 차이를 계산
- index.html 과 영상(Range 지원)을 제공

실행: python server.py  (기본 포트 8770)
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
PORT = int(os.environ.get("FRAME_ANALYZER_PORT", "8770"))
ANALYSIS_VERSION = 1          # 분석 방식이 바뀌면 올려서 캐시 무효화
SMALL_W, SMALL_H = 64, 64     # 차이 계산용 축소 크기
BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}

os.makedirs(CACHE, exist_ok=True)


# ---------------------------------------------------------------- ffmpeg
def ffmpeg_exe():
    """imageio-ffmpeg 바이너리를 cache/bin/ffmpeg.exe 로 복사해 사용 (yt-dlp가 이름으로 찾기 때문)."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    import imageio_ffmpeg
    src = imageio_ffmpeg.get_ffmpeg_exe()
    bindir = os.path.join(CACHE, "bin")
    os.makedirs(bindir, exist_ok=True)
    dst = os.path.join(bindir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
        shutil.copy2(src, dst)
    return dst


FFMPEG = ffmpeg_exe()
NOWINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def probe(path):
    """ffmpeg -i 출력에서 길이, fps, 코덱, 해상도를 읽는다 (ffprobe 없이)."""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NOWINDOW)
    err = p.stderr
    info = {"duration": 0.0, "fps": 0.0, "codec": "", "width": 0, "height": 0}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Stream #\S+.*?Video:\s*(\w+)[^\n]*", err)
    if m:
        info["codec"] = m[1]
        line = m[0]
        r = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
        if r:
            info["width"], info["height"] = int(r[1]), int(r[2])
        f = re.search(r"([\d.]+)\s*fps", line) or re.search(r"([\d.]+)\s*tbr", line)
        if f:
            info["fps"] = float(f[1])
    return info


# ---------------------------------------------------------------- jobs
JOBS = {}
JOBS_LOCK = threading.Lock()


def set_job(jid, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(jid, {}).update(kw)


def job_dir(jid):
    d = os.path.join(CACHE, jid)
    os.makedirs(d, exist_ok=True)
    return d


def find_video(d):
    for name in sorted(os.listdir(d)):
        if name.startswith("video.") and not name.endswith((".part", ".json", ".ytdl")):
            return os.path.join(d, name)
    return None


def load_cached(jid):
    p = os.path.join(CACHE, jid, "analysis.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") == ANALYSIS_VERSION and find_video(os.path.dirname(p)):
                return data
        except Exception:
            pass
    return None


def run_job(jid, kind, payload):
    try:
        cached = load_cached(jid)
        if cached:
            set_job(jid, state="done", progress=1, result=cached)
            return
        d = job_dir(jid)
        meta = {}
        if kind == "url":
            meta = download(jid, payload, d)
        else:
            meta = {"title": payload, "source": "file"}
        path = find_video(d)
        if not path:
            raise RuntimeError("영상 파일을 찾지 못했습니다.")
        info = probe(path)
        if info["codec"] and info["codec"] not in BROWSER_CODECS:
            set_job(jid, state="transcode", progress=0, message=f"{info['codec']} → H.264 변환 중")
            path = transcode(jid, path, info)
            info = probe(path)
        set_job(jid, state="analyze", progress=0, message="프레임 분석 중")
        result = analyze(jid, path, info)
        result.update(meta)
        result.update({
            "id": jid,
            "version": ANALYSIS_VERSION,
            "video": f"/media/{jid}/{os.path.basename(path)}",
            "codec": info["codec"],
            "width": info["width"], "height": info["height"],
        })
        with open(os.path.join(d, "analysis.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        set_job(jid, state="done", progress=1, result=result)
    except Exception as e:
        traceback.print_exc()
        msg = str(e)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
        set_job(jid, state="error", message=msg)


def download(jid, url, d):
    import yt_dlp

    def hook(s):
        if s.get("status") == "downloading":
            total = s.get("total_bytes") or s.get("total_bytes_estimate") or 0
            done = s.get("downloaded_bytes") or 0
            set_job(jid, state="download", progress=(done / total) if total else 0,
                    message=f"다운로드 중 {done / 1e6:.1f}MB" + (f" / {total / 1e6:.1f}MB" if total else ""))
        elif s.get("status") == "finished":
            set_job(jid, state="download", progress=1, message="다운로드 완료, 합치는 중")

    opts = {
        # 브라우저에서 바로 재생되도록 H.264(mp4)를 우선
        "format": ("bv*[vcodec^=avc1][height<=1920]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba/"
                   "b[vcodec^=avc1]/b[ext=mp4]/bv*+ba/b"),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(d, "video.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "ffmpeg_location": FFMPEG,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
    }
    cookies = os.path.join(ROOT, "cookies.txt")
    if os.path.exists(cookies):
        opts["cookiefile"] = cookies
    browser = os.environ.get("FRAME_ANALYZER_COOKIES_FROM_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    set_job(jid, state="download", progress=0, message="영상 정보 가져오는 중")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
    return {
        "title": info.get("title") or info.get("description", "")[:80] or url,
        "source": url,
        "extractor": info.get("extractor_key", ""),
        "uploader": info.get("uploader") or "",
    }


def run_ffmpeg_progress(jid, args, duration, state, message):
    """ffmpeg를 실행하며 -progress 출력으로 진행률 갱신."""
    p = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats"] + args,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", creationflags=NOWINDOW)
    err_tail = []
    t = threading.Thread(target=lambda: err_tail.extend(p.stderr.read().splitlines()[-15:]), daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                us = int(line.split("=")[1])
                set_job(jid, state=state, progress=min(1, us / 1e6 / duration), message=message)
            except ValueError:
                pass
    p.wait()
    t.join()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 실패:\n" + "\n".join(err_tail))


def transcode(jid, path, info):
    out = os.path.join(os.path.dirname(path), "video_h264.mp4")
    run_ffmpeg_progress(jid, ["-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
                              "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "veryfast",
                              "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                              "-movflags", "+faststart", out],
                        info["duration"], "transcode", f"{info['codec']} → H.264 변환 중")
    os.replace(out, os.path.join(os.path.dirname(path), "video.mp4"))
    if os.path.basename(path) != "video.mp4":
        os.remove(path)
    return os.path.join(os.path.dirname(path), "video.mp4")


def analyze(jid, path, info):
    """모든 프레임을 64x64 RGB로 디코딩해서 프레임 간 차이를 계산한다.

    d[i]  : 프레임 i-1 → i 의 평균 픽셀 차이 (0~1)
    h[i]  : 프레임 i-1 → i 의 색 히스토그램 차이 (0~1, 움직임에 둔감)
    s1[i] : 프레임 i-1 → i+1 픽셀 차이 (프레임 i 하나를 건너뛴 비교)
    s2[i] : 프레임 i-1 → i+2, s3[i] : i-1 → i+3
    → 짧게 끼워 넣은 컷은 d가 크고 sN은 작다.
    """
    frame_bytes = SMALL_W * SMALL_H * 3
    est_total = max(1, int(info["duration"] * info["fps"])) if info["fps"] else 0
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", path, "-map", "0:v:0", "-an", "-sn",
           "-fps_mode", "passthrough",
           "-vf", f"showinfo,scale={SMALL_W}:{SMALL_H}:flags=area,format=rgb24",
           "-f", "rawvideo", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWINDOW)
    pts = []
    err_tail = []

    def read_err():
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace")
            m = re.search(r"pts_time:\s*(-?[\d.]+)", line)
            if m and "showinfo" in line:
                pts.append(float(m[1]))
            else:
                err_tail.append(line.rstrip())
                del err_tail[:-15]

    t = threading.Thread(target=read_err, daemon=True)
    t.start()

    frames = []           # 축소 프레임은 64*64*3 = 12KB → 10분짜리 30fps도 200MB 정도
    hists = []
    bins = 16
    last_report = 0
    while True:
        buf = p.stdout.read(frame_bytes)
        if not buf or len(buf) < frame_bytes:
            break
        f = np.frombuffer(buf, dtype=np.uint8).reshape(SMALL_H, SMALL_W, 3)
        frames.append(f)
        q = (f >> 4).reshape(-1, 3)   # 채널별 16단계
        hist = np.concatenate([np.bincount(q[:, c], minlength=bins) for c in range(3)]).astype(np.float32)
        hists.append(hist / (SMALL_W * SMALL_H))
        n = len(frames)
        if est_total and time.time() - last_report > 0.2:
            last_report = time.time()
            set_job(jid, state="analyze", progress=min(0.99, n / est_total),
                    message=f"프레임 분석 중 {n} / ~{est_total}")
    p.wait()
    t.join()
    n = len(frames)
    if n == 0:
        raise RuntimeError("프레임을 디코딩하지 못했습니다.\n" + "\n".join(err_tail))

    stack = np.stack(frames).astype(np.int16)        # (n, H, W, 3)
    H = np.stack(hists)                               # (n, 48)

    def pixdiff(a, b):
        return np.abs(stack[a] - stack[b]).mean(axis=(1, 2, 3)) / 255.0

    d = np.zeros(n, dtype=np.float32)
    h = np.zeros(n, dtype=np.float32)
    if n > 1:
        idx = np.arange(1, n)
        d[1:] = pixdiff(idx, idx - 1)
        h[1:] = np.abs(H[1:] - H[:-1]).sum(axis=1) / 6.0   # 채널 3개 × L1 최대 2
    skips = []
    for k in (1, 2, 3):
        s = np.full(n, -1.0, dtype=np.float32)          # -1 = 계산 불가
        if n > k + 1:
            i = np.arange(1, n - k)
            s[1:n - k] = pixdiff(i + k, i - 1)
        skips.append(s)

    # pts 정리: showinfo 줄 수와 프레임 수가 다르면 fps로 채움
    fps = info["fps"] or 30.0
    if len(pts) != n:
        base = pts[0] if pts else 0.0
        pts = [base + i / fps for i in range(n)]
    if n > 2:
        diffs = np.diff(np.array(pts))
        med = float(np.median(diffs[diffs > 0])) if np.any(diffs > 0) else 1 / fps
        fps_measured = 1 / med if med > 0 else fps
    else:
        fps_measured = fps

    def r(a, nd=4):
        return [round(float(x), nd) for x in a]

    return {
        "n": n,
        "fps": round(fps_measured, 3),
        "duration": info["duration"] or (pts[-1] + 1 / fps_measured),
        "pts": r(pts, 6),
        "d": r(d), "h": r(h),
        "s1": r(skips[0]), "s2": r(skips[1]), "s3": r(skips[2]),
    }


def start_job(jid, kind, payload):
    with JOBS_LOCK:
        cur = JOBS.get(jid)
        if cur and cur.get("state") not in ("error", None):
            return  # 이미 진행 중이거나 완료
        JOBS[jid] = {"state": "queued", "progress": 0, "message": "대기 중"}
    threading.Thread(target=run_job, args=(jid, kind, payload), daemon=True).start()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/job" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self.send_file(os.path.join(ROOT, "index.html"), cache=False)
        if u.path == "/api/job":
            jid = (q.get("id") or [""])[0]
            with JOBS_LOCK:
                job = dict(JOBS.get(jid) or {})
            if not job:
                cached = load_cached(jid) if re.fullmatch(r"[0-9a-f]{16}", jid) else None
                if cached:
                    return self.send_json({"state": "done", "progress": 1, "result": cached})
                return self.send_json({"state": "error", "message": "작업을 찾을 수 없습니다."}, 404)
            return self.send_json(job)
        m = re.fullmatch(r"/media/([0-9a-f]{16})/(video[\w.]*)", u.path)
        if m:
            return self.send_file(os.path.join(CACHE, m[1], m[2]), cache=True)
        self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/url":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self.send_json({"error": "잘못된 요청"}, 400)
            url = (data.get("url") or "").strip()
            if not re.match(r"https?://", url):
                return self.send_json({"error": "http(s) 링크를 입력하세요."}, 400)
            jid = hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]
            if data.get("force"):
                shutil.rmtree(os.path.join(CACHE, jid), ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(jid, None)
            start_job(jid, "url", url)
            return self.send_json({"id": jid})
        if u.path == "/api/upload":
            length = int(self.headers.get("Content-Length") or 0)
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "video.mp4")
            ext = os.path.splitext(name)[1].lower()
            if not re.fullmatch(r"\.\w{1,5}", ext or ""):
                ext = ".mp4"
            tmp = os.path.join(CACHE, "upload_" + uuid.uuid4().hex + ext)
            sha = hashlib.sha1()
            left = length
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    sha.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
            jid = sha.hexdigest()[:16]
            d = os.path.join(CACHE, jid)
            if find_video(d) if os.path.isdir(d) else False:
                os.remove(tmp)
            else:
                os.makedirs(d, exist_ok=True)
                os.replace(tmp, os.path.join(d, "video" + ext))
            start_job(jid, "file", name)
            return self.send_json({"id": jid})
        self.send_error(404)

    def send_file(self, path, cache):
        if not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m[1]:
                    start = int(m[1])
                    if m[2]:
                        end = min(int(m[2]), size - 1)
                elif m[2]:
                    start = max(0, size - int(m[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass


def normalize_url(url):
    """같은 영상의 다른 링크 형태(추적 파라미터 등)를 같은 캐시로 묶는다."""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    keep = {"v", "list"} if "youtube" in host else set()
    qs = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k in keep])
    return f"{host}{u.path.rstrip('/')}?{qs}"


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    print(f"FrameAnalyzer: {url}  (종료: Ctrl+C)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

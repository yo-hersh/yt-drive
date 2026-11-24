"""
Downloads from YouTube with yt-dlp and moves every finished file straight to Google Drive.

Run by .github/workflows/download.yml; settings come from environment variables:
  YT_URL, YT_MODE (video / mp3 / subs), YT_QUALITY (1080 / 720 / 480 / 360 / max / small),
  YT_ITEMS (e.g. "3-7"), YT_WHOLE_PLAYLIST, YT_EMBED_SUBS ("true" / "false"),
  YT_STOP_MINUTES (stop after N minutes and upload what was recorded; for live streams),
  YT_SUB_LANGS (subtitle languages: "he", "en", "he,en", "all"), YT_ATTEMPT (1, 2, 3...),
  DRIVE_FOLDER, YT_COOKIES_FILE, YT_LANG.
Upload: DRIVE_UPLOADER = "apps_script" (DRIVE_URL + DRIVE_KEY, see drive.py)
        or "rclone" (an rclone remote named "gdrive", made with your own Google client_id).

`python download.py --post FILE` is the per-file step yt-dlp calls (--exec): fix the audio
to AAC, then upload and delete the local copy, so the runner's disk never fills up.
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from drive import Drive, DriveError, configured
from i18n import has, t

BASE_DIR = Path(os.environ.get("RUNNER_TEMP") or Path.cwd()) / "downloads"
FOLDER = (os.environ.get("DRIVE_FOLDER") or "YouTube").strip("/")
UPLOADER = os.environ.get("DRIVE_UPLOADER") or "apps_script"
RCLONE_REMOTE = "gdrive"
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY")
YTDLP = [sys.executable, "-m", "yt_dlp"]

# AAC audio (YouTube format 140) so Windows "Movies & TV" plays the sound; H.264 video when available.
VIDEO_FORMAT = "bv*+ba[acodec^=mp4a]/bv*+ba/b"
QUALITY_SORT = {"max": "res,fps,vcodec:h264", "small": "+res,+size"}
# YouTube calls Hebrew "iw"; "iw-en" is YouTube's own translation from English.
SUB_LANG_ALIASES = {"he": ["iw", "iw-en", "he"], "iw": ["iw", "iw-en", "he"], "all": ["all", "-live_chat"]}
DEFAULT_SUB_LANGS = "he"
# Playlist items go into a sub-folder named after the playlist, numbered "N - title"; single videos
# get "." instead. The "/" must stay outside the field, or --windows-filenames turns it into "⧸".
OUT_TEMPLATE = "%(playlist_title|.)s/%(playlist_index&{} - |)s%(title)s.%(ext)s"
# The rest are tried automatically only if YouTube answers "Sign in to confirm you're not a bot".
PLAYER_CLIENTS = ["default", "android", "tv_simply", "web_safari", "mweb"]
# With signed-in cookies yt-dlp ignores clients that don't support cookies (android, tv_simply...).
PLAYER_CLIENTS_SIGNED_IN = ["default", "web_safari", "mweb", "tv"]
BOT_ERROR = "Sign in to confirm"
COOKIES_ROTATED = "cookies are no longer valid"
# YouTube turning away this runner's IP for a while; a new run gets another server and IP.
SERVER_BLOCKED = ["HTTP Error 429", "HTTP Error 403", "content isn't available", BOT_ERROR]
# YouTube answers 429 to auto-translated subtitles (e.g. Hebrew from Yiddish) fetched right after
# the video page, from any IP, with or without cookies; waiting ~60 seconds first avoids it
# (https://github.com/yt-dlp/yt-dlp/issues/13831). A new server doesn't help, so no retry for it.
SUBS_SLEEP = "60"
SUBS_FAILED = "Unable to download video subtitles"
NO_SUBS = ["no subtitles for the requested languages", "has no subtitles", "has no automatic captions"]
ERROR_HINTS = [
    (COOKIES_ROTATED, "hint_cookies_rotated"),
    (BOT_ERROR, "hint_bot"),
    ("Requested format is not available", "hint_update"),
    ("HTTP Error 429", "hint_blocked"),
    ("HTTP Error 403", "hint_blocked"),
    ("content isn't available", "hint_blocked"),
]
# Automatic runs on a new server when YouTube blocks one (the first run counts as 1).
MAX_ATTEMPTS = 3
# post_process runs in a separate process (yt-dlp --exec), so successful uploads are counted in a file.
UPLOAD_LOG = BASE_DIR.parent / "yt-drive-uploaded.txt"
VIDEO_EXTS = (".mp4", ".mkv", ".webm")
# The workflow's own limit is 340 minutes; leave time for the last upload.
MAX_STOP_MINUTES = 300
_deadline = None  # time.monotonic() at which yt-dlp gets Ctrl+C
_stopped = False
_cookies_dead = False  # YouTube rejected the cookies: stop instead of a slow guest download


def _env_bool(name):
    return os.environ.get(name, "").lower() == "true"


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def _summary(line):
    if SUMMARY:
        with open(SUMMARY, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------- cookies ----------

def _cookies():
    path = os.environ.get("YT_COOKIES_FILE")
    return Path(path) if path and Path(path).is_file() and Path(path).stat().st_size > 1 else None


def _cookie_names(path):
    """Names of the youtube.com cookies in a Netscape cookies file; None if the format is wrong."""
    text = path.read_text(encoding="utf-8", errors="replace")
    names = set()
    for line in text.splitlines():
        line = line.strip().removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if line.startswith("#") or len(fields) != 7:
            continue
        if fields[0].lstrip(".").endswith("youtube.com"):
            names.add(fields[5])
    return names if text.lstrip().startswith("#") or names else None


def _signed_in(names):
    """Same test yt-dlp uses: LOGIN_INFO plus one of the SAPISID cookies."""
    return "LOGIN_INFO" in names and bool(
        names & {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"})


# ---------- per-file step: AAC fix + upload ----------

def _audio_codec(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return result.stdout.strip() or None


def _fix_audio(path):
    """Converts non-AAC audio to AAC (video copied as is). Returns the final path."""
    if _audio_codec(path) in (None, "aac"):
        return path
    print(t("fixing_audio", name=path.name), flush=True)
    tmp = path.with_name(path.stem + ".aac-tmp.mp4")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", "0:v", "-map", "0:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)])
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        print(t("fix_failed", name=path.name), flush=True)
        return path
    final = path.with_suffix(".mp4")
    path.unlink()
    tmp.replace(final)
    return final


def post_process(path):
    """Fixes the audio, moves the file to Drive (same sub-folders) and deletes the local copy."""
    path = Path(path)
    if not path.is_file():
        return True
    if path.suffix.lower() in VIDEO_EXTS:
        path = _fix_audio(path)
    rel = path.relative_to(BASE_DIR)
    print(t("uploading", name=path.name, size=_size(path.stat().st_size)), flush=True)
    upload = _upload_rclone if UPLOADER == "rclone" else _upload_apps_script
    if not upload(path, rel):
        return False
    path.unlink(missing_ok=True)
    print(t("uploaded", path=f"{FOLDER}/{rel.as_posix()}"), flush=True)
    _summary(f"- {rel.as_posix()}")
    with open(UPLOAD_LOG, "a", encoding="utf-8") as f:
        f.write(rel.as_posix() + "\n")
    return True


def _upload_apps_script(path, rel):
    try:
        drive = Drive()
        parent = drive.folder("/".join([FOLDER, *rel.parent.parts]))
        if drive.exists(path.name, parent, path.stat().st_size):
            print(t("upload_exists", name=path.name), flush=True)
        else:
            drive.upload(path, parent, lambda pct: print(t("upload_progress", pct=pct), flush=True))
    except DriveError as e:
        print(t("upload_failed", name=path.name, error=_drive_error(e)), flush=True)
        return False
    return True


def _upload_rclone(path, rel):
    # --size-only: a file already in Drive with the same size is skipped (the local copy is still removed).
    result = subprocess.run(
        ["rclone", "moveto", str(path), f"{RCLONE_REMOTE}:{FOLDER}/{rel.as_posix()}", "--size-only",
         "--drive-chunk-size", "64M", "--stats", "30s", "--stats-one-line", "--stats-log-level", "NOTICE"])
    if result.returncode != 0:
        print(t("upload_failed", name=path.name, error=t("rclone_failed", code=result.returncode)), flush=True)
        return False
    return True


def _drive_error(e):
    code = e.args[0]
    text = t(f"drive_{code}") if has(f"drive_{code}") else t("drive_other", code=code)
    return f"{text} ({e.args[1]})" if len(e.args) > 1 else text


# ---------- main run ----------

def _stop(proc):
    """Ctrl+C to yt-dlp only: on a live stream it tells ffmpeg to close the file properly and
    then runs the post-processing (AAC fix + upload) as if the stream had ended."""
    global _stopped
    if proc.poll() is None:
        _stopped = True
        print(t("time_up"), flush=True)
        proc.send_signal(signal.SIGINT)


def _run(args):
    """Runs yt-dlp with live output. Returns (exit code, full output)."""
    proc = subprocess.Popen(YTDLP + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    timer = None
    if _deadline is not None:
        timer = threading.Timer(max(0, _deadline - time.monotonic()), _stop, [proc])
        timer.daemon = True
        timer.start()
    global _cookies_dead
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
        # Without a signed-in account YouTube blocks the video piece by piece (403); better to
        # stop now and say the cookies need refreshing.
        if COOKIES_ROTATED in line and not _cookies_dead:
            _cookies_dead = True
            print(t("cookies_rotated_stop"), flush=True)
            proc.terminate()
    code = proc.wait()
    if timer:
        timer.cancel()
    return code, "".join(lines)


def _run_clients(args, url, signed_in):
    """Runs yt-dlp with each client in turn until YouTube stops asking for sign-in."""
    clients = PLAYER_CLIENTS_SIGNED_IN if signed_in else PLAYER_CLIENTS
    for i, client in enumerate(clients):
        if i:
            print(t("retry_client", prev=clients[i - 1], next=client), flush=True)
        else:
            print(t("client", client=client), flush=True)
        extra = [] if client == "default" else ["--extractor-args", f"youtube:player_client={client}"]
        code, output = _run(args + extra + [url])
        if _stopped:
            return 0, output  # stopped on purpose; whatever was recorded is already uploaded
        if _cookies_dead:
            return 1, output  # not yt-dlp's -15 from terminate()
        if BOT_ERROR not in output:
            break
    return code, output


def _stop_minutes():
    """YT_STOP_MINUTES as a whole number of minutes (0 = no limit), capped at MAX_STOP_MINUTES."""
    raw = (os.environ.get("YT_STOP_MINUTES") or "").strip()
    if not raw:
        return 0
    try:
        minutes = int(float(raw))
    except ValueError:
        print(t("time_limit_bad", value=raw))
        return 0
    if minutes > MAX_STOP_MINUTES:
        print(t("time_limit_capped", minutes=MAX_STOP_MINUTES))
        minutes = MAX_STOP_MINUTES
    return max(minutes, 0)


def _sub_langs():
    """(yt-dlp --sub-langs value, the languages as typed) from YT_SUB_LANGS, e.g. "he,en"."""
    raw = (os.environ.get("YT_SUB_LANGS") or "").replace(" ", "").lower()
    codes = [c for c in raw.split(",") if re.fullmatch(r"[a-z0-9-]+", c)] or [DEFAULT_SUB_LANGS]
    langs = []
    for code in codes:
        for lang in SUB_LANG_ALIASES.get(code, [code]):
            if lang not in langs:
                langs.append(lang)
    return ",".join(langs), ",".join(codes)


def _attempt():
    try:
        return max(int(os.environ.get("YT_ATTEMPT") or 1), 1)
    except ValueError:
        return 1


def _uploaded_count():
    try:
        return len(UPLOAD_LOG.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def _build_args():
    mode = os.environ.get("YT_MODE") or "video"
    quality = os.environ.get("YT_QUALITY") or "1080"
    items = (os.environ.get("YT_ITEMS") or "").strip()
    url = os.environ["YT_URL"].strip()

    if mode == "mp3":
        print(t("start_audio", url=url))
    elif mode == "subs":
        print(t("start_subs", url=url))
    else:
        print(t("start_video", quality=quality, url=url))
    print(t("target", target=FOLDER))

    # A blocked fragment (HTTP 403) won't come back: fail fast instead of retrying it 10 times
    # for each of hundreds of fragments.
    args = ["--ignore-errors", "--windows-filenames", "--newline", "--progress-delta", "5",
            "-P", str(BASE_DIR), "-o", OUT_TEMPLATE, "--embed-metadata",
            "--fragment-retries", "3", "--abort-on-unavailable-fragments"]
    if items:
        print(t("items", items=items))
        args += ["--yes-playlist", "--playlist-items", items]
    elif not _env_bool("YT_WHOLE_PLAYLIST"):
        args += ["--no-playlist"]  # a link with both v= and list= downloads only that video
    stop = _stop_minutes()
    if stop:
        global _deadline
        _deadline = time.monotonic() + stop * 60
        print(t("time_limit", minutes=stop))
        # ffmpeg (instead of yt-dlp's own HLS/DASH downloader) can close a live recording cleanly.
        args += ["--downloader", "m3u8:ffmpeg", "--downloader", "dash:ffmpeg",
                 "--downloader-args", "ffmpeg:-hide_banner -loglevel error -stats"]
    if items or _env_bool("YT_WHOLE_PLAYLIST"):
        # A pause between videos, as yt-dlp advises with signed-in cookies, so the account isn't flagged.
        args += ["--sleep-interval", "4", "--max-sleep-interval", "10"]

    signed_in = False
    cookies = _cookies()
    if cookies:
        print(t("cookies_used"))
        names = _cookie_names(cookies)
        if names is None:
            print(t("cookies_bad_format"))
        else:
            signed_in = _signed_in(names)
            print(t("cookies_logged_in") if signed_in else t("cookies_not_logged_in"))
        args += ["--cookies", str(cookies)]

    sub_langs, sub_codes = _sub_langs()
    if mode == "subs" or (mode == "video" and _env_bool("YT_EMBED_SUBS")):
        print(t("sub_langs", langs=sub_codes))
    if mode == "subs":
        args += ["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", sub_langs,
                 "--convert-subs", "srt", "--sleep-subtitles", SUBS_SLEEP, "--sleep-requests", "3",
                 "--retries", "20", "--retry-sleep", "15"]
    else:
        post = " ".join(shlex.quote(p) for p in (sys.executable, str(Path(__file__).resolve()), "--post"))
        args += ["--exec", f"after_move:{post}"]
        if mode == "mp3":
            args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            sort = QUALITY_SORT.get(quality, f"res:{quality},fps,vcodec:h264")
            args += ["-f", VIDEO_FORMAT, "-S", sort, "--merge-output-format", "mp4"]
            if _env_bool("YT_EMBED_SUBS"):
                args += ["--write-subs", "--write-auto-subs", "--sub-langs", sub_langs,
                         "--convert-subs", "srt", "--embed-subs",
                         "--sleep-subtitles", SUBS_SLEEP, "--retries", "20", "--retry-sleep", "15"]
    return args, url, signed_in, mode, sub_codes


def main():
    if not (os.environ.get("YT_URL") or "").strip():
        print(t("no_url"))
        return 1
    # Fail fast on a missing / broken Drive connection, before downloading anything.
    if UPLOADER == "rclone":
        remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
        if f"{RCLONE_REMOTE}:" not in remotes:
            print(t("no_drive_rclone"))
            return 1
    else:
        if not configured():
            print(t("no_drive"))
            return 1
        try:
            Drive().folder(FOLDER)
        except DriveError as e:
            print(_drive_error(e))
            return 1
    print(t("uploader", name=UPLOADER))

    version = subprocess.run(YTDLP + ["--version"], capture_output=True, text=True).stdout.strip()
    print(t("ytdlp_version", version=version))
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_LOG.unlink(missing_ok=True)
    attempt = _attempt()
    if attempt > 1:
        print(t("attempt", attempt=attempt, max=MAX_ATTEMPTS))
    _summary(f'<div dir="{t("dir")}">\n\n{t("summary_title")}\n\n{t("summary_files", folder=FOLDER)}\n')

    args, url, signed_in, mode, sub_codes = _build_args()
    print("-" * 60, flush=True)
    code, output = _run_clients(args, url, signed_in)
    print("-" * 60, flush=True)

    # Anything --exec did not upload (subtitle files, a failed upload) gets one more try.
    for path in sorted(p for p in BASE_DIR.rglob("*") if p.is_file()):
        if not post_process(path):
            code = code or 1

    messages = []
    # yt-dlp may end with code 0 and nothing downloaded (a subtitle download refused with 429 is
    # only a warning); that is not a success.
    nothing = _uploaded_count() == 0 and not _stopped
    if nothing:
        code = code or 1
        messages.append(t("nothing_uploaded"))
    messages.insert(0, t("done") if code == 0 else t("done_errors", code=code))
    if code:
        shown = set()
        # Subtitle failures get their own hint; only the rest tells whether the server was blocked.
        subs_lines = [line for line in output.splitlines() if SUBS_FAILED in line]
        rest = "\n".join(line for line in output.splitlines() if SUBS_FAILED not in line)
        for fragment, key in ERROR_HINTS:
            if fragment in rest and key not in shown:
                shown.add(key)
                messages.append(t(key))
        if any("429" in line for line in subs_lines):
            messages.append(t("hint_subs_429"))
        blocked = any(s in rest for s in SERVER_BLOCKED) and not _cookies_dead
        if nothing and mode == "subs" and not blocked and any(s in output for s in NO_SUBS):
            messages.append(t("hint_no_subs", langs=sub_codes))
        if blocked and nothing:
            if attempt < MAX_ATTEMPTS:
                _set_output("retry", "true")
                _set_output("next_attempt", attempt + 1)
                messages.append(t("retry_new_server", next=attempt + 1, max=MAX_ATTEMPTS))
            else:
                messages.append(t("retry_exhausted", max=MAX_ATTEMPTS))
    for m in messages:
        print(m)
    _summary("\n" + "\n\n".join(messages) + "\n\n</div>")
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    return code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--post":
        sys.exit(0 if post_process(sys.argv[2]) else 1)
    sys.exit(main())

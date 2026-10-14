"""
Downloads from YouTube with yt-dlp and moves every finished file straight to Google Drive.

Run by .github/workflows/download.yml; settings come from environment variables:
  YT_URL, YT_MODE (video / mp3 / subs), YT_QUALITY (1080 / 720 / 480 / 360 / max / small),
  YT_ITEMS (e.g. "3-7"), YT_WHOLE_PLAYLIST, YT_EMBED_SUBS ("true" / "false"),
  DRIVE_FOLDER, YT_COOKIES_FILE, YT_LANG; DRIVE_URL + DRIVE_KEY for Drive (see drive.py).

`python download.py --post FILE` is the per-file step yt-dlp calls (--exec): fix the audio
to AAC, then upload and delete the local copy, so the runner's disk never fills up.
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from drive import Drive, DriveError, configured
from i18n import has, t

BASE_DIR = Path(os.environ.get("RUNNER_TEMP") or Path.cwd()) / "downloads"
FOLDER = (os.environ.get("DRIVE_FOLDER") or "YouTube").strip("/")
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY")
YTDLP = [sys.executable, "-m", "yt_dlp"]

# AAC audio (YouTube format 140) so Windows "Movies & TV" plays the sound; H.264 video when available.
VIDEO_FORMAT = "bv*+ba[acodec^=mp4a]/bv*+ba/b"
QUALITY_SORT = {"max": "res,fps,vcodec:h264", "small": "+res,+size"}
SUB_LANGS = "iw,iw-en,he"
# Playlist items go into a sub-folder named after the playlist, numbered "N - title".
OUT_TEMPLATE = "%(playlist_title&{}/|)s%(playlist_index&{} - |)s%(title)s.%(ext)s"
# The rest are tried automatically only if YouTube answers "Sign in to confirm you're not a bot".
PLAYER_CLIENTS = ["default", "android", "tv_simply", "web_safari", "mweb"]
# With signed-in cookies yt-dlp ignores clients that don't support cookies (android, tv_simply...).
PLAYER_CLIENTS_SIGNED_IN = ["default", "web_safari", "mweb", "tv"]
BOT_ERROR = "Sign in to confirm"
ERROR_HINTS = [
    ("cookies are no longer valid", "hint_cookies_rotated"),
    (BOT_ERROR, "hint_bot"),
    ("Requested format is not available", "hint_update"),
    ("HTTP Error 403", "hint_update"),
]
VIDEO_EXTS = (".mp4", ".mkv", ".webm")


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
    size = path.stat().st_size
    try:
        drive = Drive()
        parent = drive.folder("/".join([FOLDER, *rel.parent.parts]))
        if drive.exists(path.name, parent, size):
            print(t("upload_exists", name=path.name), flush=True)
        else:
            print(t("uploading", name=path.name, size=_size(size)), flush=True)
            drive.upload(path, parent, lambda pct: print(t("upload_progress", pct=pct), flush=True))
            print(t("uploaded", path=f"{FOLDER}/{rel.as_posix()}"), flush=True)
    except DriveError as e:
        print(t("upload_failed", name=path.name, error=_drive_error(e)), flush=True)
        return False
    path.unlink()
    _summary(f"- {rel.as_posix()}")
    return True


def _drive_error(e):
    code = e.args[0]
    text = t(f"drive_{code}") if has(f"drive_{code}") else t("drive_other", code=code)
    return f"{text} ({e.args[1]})" if len(e.args) > 1 else text


# ---------- main run ----------

def _run(args):
    """Runs yt-dlp with live output. Returns (exit code, full output)."""
    proc = subprocess.Popen(YTDLP + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    lines = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return proc.wait(), "".join(lines)


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
        if BOT_ERROR not in output:
            break
    return code, output


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

    args = ["--ignore-errors", "--windows-filenames", "--newline", "--progress-delta", "5",
            "-P", str(BASE_DIR), "-o", OUT_TEMPLATE, "--embed-metadata"]
    if items:
        print(t("items", items=items))
        args += ["--yes-playlist", "--playlist-items", items]
    elif not _env_bool("YT_WHOLE_PLAYLIST"):
        args += ["--no-playlist"]  # a link with both v= and list= downloads only that video
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

    if mode == "subs":
        args += ["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", SUB_LANGS,
                 "--convert-subs", "srt", "--sleep-subtitles", "8", "--sleep-requests", "3",
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
                args += ["--write-subs", "--write-auto-subs", "--sub-langs", SUB_LANGS,
                         "--convert-subs", "srt", "--embed-subs",
                         "--sleep-subtitles", "3", "--retries", "20", "--retry-sleep", "15"]
    return args, url, signed_in


def main():
    if not (os.environ.get("YT_URL") or "").strip():
        print(t("no_url"))
        return 1
    if not configured():
        print(t("no_drive"))
        return 1
    try:
        Drive().folder(FOLDER)  # fails fast on a bad key / missing permission, before downloading
    except DriveError as e:
        print(_drive_error(e))
        return 1

    version = subprocess.run(YTDLP + ["--version"], capture_output=True, text=True).stdout.strip()
    print(t("ytdlp_version", version=version))
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    _summary(f'<div dir="{t("dir")}">\n\n{t("summary_title")}\n\n{t("summary_files", folder=FOLDER)}\n')

    args, url, signed_in = _build_args()
    print("-" * 60, flush=True)
    code, output = _run_clients(args, url, signed_in)
    print("-" * 60, flush=True)

    # Anything --exec did not upload (subtitle files, a failed upload) gets one more try.
    for path in sorted(p for p in BASE_DIR.rglob("*") if p.is_file()):
        if not post_process(path):
            code = code or 1

    messages = [t("done") if code == 0 else t("done_errors", code=code)]
    shown = set()
    for fragment, key in ERROR_HINTS:
        if fragment in output and key not in shown:
            shown.add(key)
            messages.append(t(key))
    for m in messages:
        print(m)
    _summary("\n" + "\n\n".join(messages) + "\n\n</div>")
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    return code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--post":
        sys.exit(0 if post_process(sys.argv[2]) else 1)
    sys.exit(main())

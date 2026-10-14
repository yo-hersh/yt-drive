"""
Google Drive upload without a Google Cloud project: the token comes from the Apps Script web app
in apps-script/ (DRIVE_URL + DRIVE_KEY), then files go up with Drive's resumable upload API.
"""
import os
import time

import requests

# Pasted secrets may carry quotes, a BOM or line breaks.
_JUNK = " \r\n\t\"'﻿"
DRIVE_URL = os.environ.get("DRIVE_URL", "").strip(_JUNK)
DRIVE_KEY = os.environ.get("DRIVE_KEY", "").strip(_JUNK)
FILES_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id"
FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK = 64 * 1024 * 1024  # must be a multiple of 256 KB
RETRIES = 6


class DriveError(Exception):
    """args[0] is a stable error code (bad_key, http_403...), translated by the caller."""


def configured():
    return bool(DRIVE_URL and DRIVE_KEY)


class Drive:
    def __init__(self):
        self.session = requests.Session()
        self._token = None
        self._token_time = 0

    # ---------- auth ----------

    def _refresh(self):
        # Apps Script answers with a redirect; requests follows it and gets the JSON.
        try:
            r = requests.post(DRIVE_URL, json={"key": DRIVE_KEY}, timeout=60)
        except requests.RequestException as e:
            raise DriveError("token_request", type(e).__name__)
        try:
            data = r.json()
        except ValueError:  # an HTML page: not deployed, or access isn't "Anyone"
            raise DriveError("no_token")
        if "token" not in data:
            raise DriveError(data.get("error", "no_token"))
        self._token, self._token_time = data["token"], time.time()

    def _headers(self, extra=None):
        if not self._token or time.time() - self._token_time > 45 * 60:  # tokens live 1 hour
            self._refresh()
        return {"Authorization": f"Bearer {self._token}", **(extra or {})}

    def _call(self, method, url, headers=None, **kw):
        """Request with a fresh token on 401 and backoff on 429 / 5xx / network errors."""
        r = error = None
        for attempt in range(RETRIES):
            auth = self._headers(headers)  # DriveError here (no token) is not worth retrying
            try:
                r = self.session.request(method, url, headers=auth, timeout=300, **kw)
            except requests.RequestException as e:
                r, error = None, type(e).__name__
            if r is not None and r.status_code not in (401, 429, 500, 502, 503, 504):
                return r
            if r is not None and r.status_code == 401:
                self._token = None
            time.sleep(min(2 ** attempt, 30))
        if r is None:
            raise DriveError("network", error)
        return r

    # ---------- folders ----------

    @staticmethod
    def _q(name):
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def find(self, name, parent, mime=None):
        q = f"name = '{self._q(name)}' and '{parent}' in parents and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        r = self._call("GET", FILES_API, params={"q": q, "fields": "files(id,size)", "pageSize": 10})
        if r.status_code != 200:
            raise DriveError(f"http_{r.status_code}")
        return r.json().get("files", [])

    def folder(self, path):
        """Id of a folder path like "YouTube/My playlist", created as needed."""
        parent = "root"
        for name in [p for p in path.split("/") if p]:
            found = self.find(name, parent, FOLDER_MIME)
            if found:
                parent = found[0]["id"]
                continue
            r = self._call("POST", FILES_API, params={"fields": "id"},
                           json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]})
            if r.status_code != 200:
                raise DriveError(f"http_{r.status_code}")
            parent = r.json()["id"]
        return parent

    # ---------- upload ----------

    def exists(self, name, parent, size):
        return any(int(f.get("size", -1)) == size for f in self.find(name, parent))

    def upload(self, path, parent, progress=None):
        """Resumable upload of a local file into the folder id `parent`."""
        size = path.stat().st_size
        r = self._call("POST", UPLOAD_API, json={"name": path.name, "parents": [parent]},
                       headers={"X-Upload-Content-Length": str(size)})
        if r.status_code != 200:
            raise DriveError(f"http_{r.status_code}")
        session_url = r.headers["Location"]
        offset = stalled = 0
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(CHUNK)
                end = offset + len(data) - 1
                rng = f"bytes {offset}-{end}/{size}" if data else f"bytes */{size}"
                try:
                    r = self._call("PUT", session_url, data=data, headers={"Content-Range": rng})
                except DriveError:
                    r = None
                if r is not None and r.status_code in (200, 201):
                    return
                if r is None or r.status_code != 308:
                    # Ask Drive how much it already has, then continue from there.
                    r = self._call("PUT", session_url, headers={"Content-Range": f"bytes */{size}"})
                    if r.status_code in (200, 201):
                        return
                    if r.status_code != 308:
                        raise DriveError(f"http_{r.status_code}")
                # "Range: bytes=0-N" says how much Drive actually has; continue from there.
                got = r.headers.get("Range")
                new_offset = int(got.rsplit("-", 1)[1]) + 1 if got else 0
                stalled = stalled + 1 if new_offset <= offset else 0
                if stalled >= RETRIES:
                    raise DriveError("stalled")
                offset = new_offset
                if progress:
                    progress(offset * 100 // size)

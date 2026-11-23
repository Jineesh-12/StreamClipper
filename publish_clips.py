import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://api.uplika.com"
PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / ".uplika_published.json"
MAX_UPLOAD_MB = 300  # Allow up to 300 MB before auto-compressing


class _FileProgressReader:
    """Streams an open binary file in chunks while printing clean ASCII progress."""

    def __init__(self, file_obj, total_size, chunk_size=2097152):
        self._f = file_obj
        self.size = total_size
        self.chunk_size = chunk_size
        self.sent = 0
        self._last_pct = -5
        self._print_progress(force=True)

    def _format_bar(self, pct: int, width: int = 12) -> str:
        filled = max(0, min(width, int(round((pct * width) / 100))))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def _format_mb(self, byte_count: int) -> str:
        mb = byte_count / (1024 * 1024)
        return f"{mb:.1f} MB" if self.size < 10 * 1024 * 1024 else f"{mb:.0f} MB"

    def _print_progress(self, force=False):
        pct = 100 if self.size <= 0 else min(100, int((self.sent * 100) / self.size))
        if force or pct == 100 or (pct - self._last_pct) >= 5:
            self._last_pct = pct
            sys.stdout.write(
                f"\rUploading: {self._format_bar(pct)} {pct:3d}%  |  Uploaded: {self._format_mb(self.sent)} / {self._format_mb(self.size)}"
            )
            sys.stdout.flush()

    def __len__(self):
        return self.size

    def read(self, n=-1):
        to_read = self.chunk_size if (n is None or n < 0) else min(n, self.chunk_size)
        data = self._f.read(to_read)
        if data:
            self.sent += len(data)
            self._print_progress()
        return data

    def finish_line(self):
        self.sent = self.size
        self._print_progress(force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


def load_api_key():
    env_file = PROJECT_DIR / ".env"

    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("UPLIKA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    return os.environ.get("UPLIKA_API_KEY", "").strip()


def api_request(method, endpoint, data=None, headers=None):
    request_headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = Request(
        BASE_URL + endpoint,
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Uplika API error {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Network error: {error}") from error


def unwrap(result):
    if isinstance(result, dict) and "data" in result and isinstance(result["data"], (dict, list)):
        return result["data"]
    return result


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("Warning: saved upload history could not be read.")
    return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def clip_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(2 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_upload_file(video_path: Path, max_mb: int = MAX_UPLOAD_MB):
    """Returns (path_to_upload, temp_dir_or_None). Compresses if over max_mb."""
    size_bytes = video_path.stat().st_size
    limit_bytes = max_mb * 1024 * 1024

    if size_bytes <= limit_bytes or not shutil.which("ffmpeg"):
        return video_path, None

    tmp_dir = Path(tempfile.mkdtemp(prefix="uplika_small_"))
    out_file = tmp_dir / video_path.name

    duration_sec = None
    if shutil.which("ffprobe"):
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        try:
            duration_sec = max(1.0, float(probe.stdout.strip()))
        except ValueError:
            duration_sec = None

    print(f"Clip is {size_bytes / (1024*1024):.1f} MB (> {max_mb} MB limit). Compressing for upload...")

    if duration_sec:
        target_total_kbps = int(((limit_bytes * 0.85) * 8) / duration_sec / 1000)
        audio_kbps = 96
        video_kbps = max(250, target_total_kbps - audio_kbps)
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", "scale='min(720,iw)':-2",
            "-c:v", "libx264", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-preset", "fast",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            str(out_file),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", "scale='min(720,iw)':-2",
            "-c:v", "libx264", "-crf", "28", "-preset", "fast",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(out_file),
        ]

    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
        new_mb = out_file.stat().st_size / (1024 * 1024)
        print(f"Compressed to {new_mb:.1f} MB.")
        return out_file, tmp_dir

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return video_path, None


def read_clip_text(txt_path):
    text = txt_path.read_text(encoding="utf-8-sig")

    title_match = re.search(
        r"(?im)^TITLE\s*:\s*(.+?)\s*$",
        text,
    )
    description_match = re.search(
        r"(?ims)^DESCRIPTION\s*:\s*\n(.*?)(?:\n-{5,}|\Z)",
        text,
    )

    if not title_match or not description_match:
        raise ValueError("The TXT file is missing TITLE or DESCRIPTION.")

    title = title_match.group(1).strip()
    description = description_match.group(1).strip()

    if not title or not description:
        raise ValueError("The title or description is empty.")

    return title[:100], description


def get_accounts():
    result = unwrap(api_request("GET", "/v1/accounts"))
    if isinstance(result, dict):
        result = result.get("accounts", [])

    if not isinstance(result, list):
        raise RuntimeError("Unexpected account-list response from Uplika.")

    accounts = {}
    for account in result:
        platform = str(account.get("platform", "")).lower()
        status = str(account.get("status", "")).lower()

        if platform in ("youtube", "instagram") and status in (
            "", "active", "connected", "healthy"
        ):
            accounts[platform] = account

    return accounts


def _extract_session_fields(session_obj):
    if not isinstance(session_obj, dict):
        return None, None, None, "video/mp4"

    media_id = (
        session_obj.get("mediaId")
        or session_obj.get("media_id")
        or session_obj.get("id")
    )
    session_id = (
        session_obj.get("sessionId")
        or session_obj.get("session_id")
        or session_obj.get("id")
    )
    upload_url = (
        session_obj.get("uploadUrl")
        or session_obj.get("upload_url")
        or session_obj.get("putUrl")
        or session_obj.get("put_url")
        or session_obj.get("url")
    )
    content_type = session_obj.get("contentType") or session_obj.get("content_type") or "video/mp4"
    return media_id, session_id, upload_url, content_type


def upload_media(video_path):
    upload_path, tmp_dir = prepare_upload_file(video_path, max_mb=MAX_UPLOAD_MB)
    size = upload_path.stat().st_size

    if size > 300 * 1024 * 1024:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError(
            "This clip is over 300 MB. It may exceed Instagram's upload limit."
        )

    max_attempts = 5
    last_error = None

    try:
        import requests as _req
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = _req.Session()
        retry_cfg = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=1.0,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["PUT"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg, pool_connections=2, pool_maxsize=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except ImportError:
        session = None

    try:
        for attempt in range(1, max_attempts + 1):
            try:
                session_raw = api_request(
                    "POST",
                    "/v1/media/upload-sessions",
                    {
                        "fileName": video_path.name,
                        "contentType": "video/mp4",
                        "bytes": size,
                        "size": size,
                    },
                )
                session_data = unwrap(session_raw)
                media_id, session_id, upload_url, content_type = _extract_session_fields(session_data)

                if not upload_url and isinstance(session_raw, dict):
                    _, _, upload_url, content_type = _extract_session_fields(session_raw)

                if not upload_url:
                    raise RuntimeError(
                        f"Upload session did not return an upload URL. Response keys: {list(session_data.keys()) if isinstance(session_data, dict) else type(session_data)}"
                    )

                with upload_path.open("rb") as f_stream:
                    reader = _FileProgressReader(
                        f_stream,
                        total_size=size,
                        chunk_size=2 * 1024 * 1024,
                    )

                    if session is not None:
                        resp = session.put(
                            upload_url,
                            data=reader,
                            headers={
                                "Content-Type": content_type,
                                "Content-Length": str(size),
                            },
                            timeout=(20, 180),
                        )
                        if resp.status_code not in (200, 201, 204):
                            raise RuntimeError(f"Video upload failed (HTTP {resp.status_code}): {resp.text[:200]}")
                    else:
                        put_request = Request(
                            upload_url,
                            data=reader,
                            headers={
                                "Content-Type": content_type,
                                "Content-Length": str(size),
                            },
                            method="PUT",
                        )
                        with urlopen(put_request, timeout=180) as response:
                            if response.status not in (200, 201, 204):
                                raise RuntimeError("The video file upload was not accepted.")

                    reader.finish_line()

                if session_id:
                    for complete_path in (
                        f"/v1/media/upload-sessions/{session_id}/complete",
                        f"/v1/media/upload-sessions/{session_id}",
                    ):
                        try:
                            completed = unwrap(api_request("POST", complete_path, {}))
                            c_media_id, _, _, _ = _extract_session_fields(completed)
                            if c_media_id:
                                media_id = c_media_id
                            break
                        except RuntimeError as err:
                            if "404" in str(err) or "405" in str(err):
                                continue
                            raise

                if media_id:
                    try:
                        api_request("POST", f"/v1/media/{media_id}/complete", {})
                    except RuntimeError as err:
                        if "404" not in str(err) and "409" not in str(err):
                            raise

                if not media_id:
                    raise RuntimeError("Could not determine media ID from upload session response.")

                return media_id

            except KeyboardInterrupt:
                raise
            except Exception as error:
                sys.stdout.write("\n")
                sys.stdout.flush()
                last_error = error
                err_text = str(error)

                if attempt >= max_attempts:
                    break

                if "409" in err_text or "upload_bytes_missing" in err_text:
                    wait_sec = 135
                    print(f"[Attempt {attempt}/{max_attempts}] Uplika busy (409 stale lock). Retrying automatically...")
                elif "429" in err_text:
                    wait_sec = 30 * attempt
                    print(f"[Attempt {attempt}/{max_attempts}] Rate limited (429). Retrying automatically...")
                else:
                    wait_sec = 4 * attempt
                    print(f"[Attempt {attempt}/{max_attempts}] Upload failed ({err_text[:100]}). Retrying automatically...")

                time.sleep(wait_sec)
    finally:
        if session is not None:
            session.close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    raise RuntimeError(f"Video upload failed after {max_attempts} automatic attempts: {last_error}")


def publish_video(media_id, title, description, accounts, targets, file_hash):
    account_ids = [accounts[target]["id"] for target in targets]

    options = {}
    if "youtube" in targets:
        options["youtube"] = {
            "title": title[:100],
            "privacyStatus": "public",
            "defaultLanguage": "en",
            "content": description,
        }

    if "instagram" in targets:
        options["instagram"] = {
            "contentType": "reel",
            "content": description,
        }

    payload = {
        "content": description,
        "accountIds": account_ids,
        "mediaIds": [media_id],
        "options": options,
        "wait": True,
    }

    result = unwrap(api_request(
        "POST",
        "/v1/posts",
        payload,
        headers={"Idempotency-Key": file_hash},
    ))

    post_id = result.get("id") if isinstance(result, dict) else None

    if not post_id:
        raise RuntimeError(
            "Uplika did not return a post ID. Publication is not confirmed."
        )

    print(f"Uplika post created: {post_id}")
    print("Checking publication status...")

    for _ in range(25):
        status_result = unwrap(api_request("GET", f"/v1/posts/{post_id}"))
        if isinstance(status_result, dict):
            post_status = str(status_result.get("status", "")).lower()
            target_results = status_result.get("targets", [])

            if isinstance(target_results, list) and target_results:
                statuses = {
                    str(item.get("platform", "")).lower():
                    str(item.get("status", "")).lower()
                    for item in target_results
                }

                if all(statuses.get(target) == "published" for target in targets):
                    for item in target_results:
                        if str(item.get("platform", "")).lower() in targets:
                            url = item.get("url")
                            if url:
                                print(f"Published: {url}")
                    return True

                if any(statuses.get(target) in ("failed", "error") for target in targets):
                    print("At least one platform reported a publishing failure.")
                    for item in target_results:
                        if str(item.get("platform", "")).lower() in targets:
                            print(item.get("platform"), item.get("status"), item.get("error", ""))
                    return False

            if post_status in ("failed", "error"):
                print("Uplika reported a publishing failure.")
                return False

        time.sleep(3)

    print("Publication is still processing. Check your Uplika dashboard.")
    print("This clip has NOT been marked as successfully published locally.")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Publish new StreamClipper videos through Uplika."
    )
    parser.add_argument("--folder", required=True, help="New clip batch folder")
    parser.add_argument(
        "--target",
        required=True,
        choices=["1", "2", "3", "youtube", "instagram", "both"],
        help="1 YouTube, 2 Instagram, 3 both",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Publish all clips automatically without per-clip confirmation",
    )
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        return 1

    target_map = {
        "1": ["youtube"],
        "2": ["instagram"],
        "3": ["youtube", "instagram"],
        "youtube": ["youtube"],
        "instagram": ["instagram"],
        "both": ["youtube", "instagram"],
    }
    targets = target_map[args.target]

    videos = sorted(folder.glob("*.mp4"))
    if not videos:
        print("No MP4 clips found in this batch folder.")
        return 0

    accounts = get_accounts()
    for target in targets:
        if target not in accounts:
            print(f"No active connected {target} account was found.")
            return 1

    auto_mode = args.auto
    if not auto_mode:
        auto_ans = input("\nYou want to publish all these clips automatically? (y/n): ").strip().lower()
        auto_mode = auto_ans in ("y", "yes")

    state = load_state()
    print(f"\nFound {len(videos)} video(s) in: {folder}")
    print("Selected platforms:", ", ".join(targets))
    print("Mode:", "AUTOMATIC (no confirmation per clip)" if auto_mode else "MANUAL (confirm each clip)")
    print("YouTube visibility: PUBLIC")

    published_count = 0
    failed_count = 0

    for video in videos:
        txt_path = video.with_suffix(".txt")
        print("\n" + "=" * 55)
        print("Clip:", video.name)

        if not txt_path.exists():
            print("Skipped: matching TXT file not found.")
            continue

        try:
            title, description = read_clip_text(txt_path)
            fingerprint = clip_hash(video)
        except Exception as error:
            print("Skipped:", error)
            continue

        already_done = state.get(fingerprint, [])
        remaining = [target for target in targets if target not in already_done]

        if not remaining:
            print("Skipped: this clip was already published to the selected platforms.")
            continue

        print("Title:", title)
        print("Platforms still to publish:", ", ".join(remaining))

        if not auto_mode:
            print("\nDescription preview:")
            print(description[:500])
            answer = input("\nPublish this clip now? Type YES to confirm: ").strip().lower()
            if answer not in ("yes", "y"):
                print("Skipped by you.")
                continue

        try:
            media_id = upload_media(video)
            success = publish_video(
                media_id, title, description, accounts,
                remaining, fingerprint
            )

            if success:
                state[fingerprint] = sorted(set(already_done + remaining))
                save_state(state)
                published_count += 1
                print("Saved upload history.")
            else:
                failed_count += 1
        except Exception as error:
            failed_count += 1
            print("Upload/publishing error:", error)
            print("This clip has not been marked as successfully published. Continuing to next clip...")

    print("\n" + "=" * 55)
    print(f"Batch complete! Published: {published_count} | Failed/Pending: {failed_count}")
    return 0


API_KEY = load_api_key()

if __name__ == "__main__":
    if not API_KEY:
        print("UPLIKA_API_KEY was not found in your project's .env file.")
        sys.exit(1)
    try:
        sys.exit(main())
    except Exception as error:
        print("\nUploader stopped:", error)
        sys.exit(1)
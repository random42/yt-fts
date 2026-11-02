
import os
import sys
import json
import random
import re
import sqlite3
import tempfile
import threading
import time

import requests
import yt_dlp
import click

from pathlib import Path
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from xml.etree import ElementTree

from ..config import get_db_path
from ..db_utils import (
    add_channel_info,
    check_if_channel_exists,
    get_channel_id_from_input,
    get_vid_ids_by_channel_id,
    get_channels
)

from ..utils import parse_vtt, get_date, handle_reject_consent_cookie

from rich.console import Console


class DownloadHandler:
    _pace_lock = threading.Lock()
    _next_request_at = 0.0
    _cooldown_until = 0.0
    _request_interval = 1.0

    @classmethod
    def _pace_request(cls) -> None:
        while True:
            with cls._pace_lock:
                now = time.monotonic()
                ready = max(cls._next_request_at, cls._cooldown_until)
                if ready <= now:
                    cls._next_request_at = now + cls._request_interval
                    return
                wait = ready - now
            time.sleep(wait)

    @classmethod
    def _rate_limit_cooldown(cls, seconds: float) -> None:
        with cls._pace_lock:
            cls._cooldown_until = max(cls._cooldown_until, time.monotonic() + seconds)

    def _youtube_dl(self, options):
        handler = self

        class PacedYoutubeDL(yt_dlp.YoutubeDL):
            def urlopen(self, request):
                handler._pace_request()
                try:
                    return super().urlopen(request)
                except Exception as error:
                    if "429" in str(error) or "too many requests" in str(error).lower():
                        handler._rate_limit_cooldown(10)
                    raise

        return PacedYoutubeDL(options)

    def __init__(self, number_of_jobs: int = 2, language: str = 'en', cookies_from_browser: str | None = None) -> None:

        self.console = Console()

        self.cookies_from_browser = cookies_from_browser
        self.number_of_jobs = number_of_jobs
        self.language = language

        self.session: requests.Session | None = None
        self.channel_id: str | None = None
        self.channel_name: str | None = None
        self.video_ids: list[str] | None = None
        self.tmp_dir: str | None = None

    def download_channel(self, url: str) -> None:

        try:
            channel_url = self.validate_channel_url(url)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        self.channel_id = self.get_channel_id(channel_url)
        if not self.channel_name:
            try:
                self.session = self.init_session(channel_url)
                self.channel_name = self.get_channel_name(self.channel_id)
            except requests.RequestException as e:
                raise click.ClickException(f"Could not get channel name: {e}") from e

        if check_if_channel_exists(self.channel_id):
            self.console.print(f"[yellow]Channel '{self.channel_name}' already exists in database. Updating instead...[/yellow]")
            self.update_channel(self.channel_id)
            return

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.tmp_dir = tmp_dir
            channel_url = f"https://www.youtube.com/channel/{self.channel_id}/videos"
            self.video_ids = self.get_videos_list(channel_url)

            self.console.print(
                f"[green]Downloading [red]{len(self.video_ids)}[/red] "
                "vtt files[/green]"
            )

            add_channel_info(self.channel_id, self.channel_name, channel_url)
            self.download_vtts()

        self.console.print(f"[green]Finished downloading subtitles for {self.channel_name}[/green]")

    def download_playlist(self, playlist_url: str, language: str, number_of_jobs: int) -> None:
        self.language = language
        self.number_of_jobs = number_of_jobs

        playlist_data = self.get_playlist_data(playlist_url)

        for video in playlist_data:
            channel_name = video["channel_name"]
            channel_id = video["channel_id"]
            channel_url = video["channel_url"]
            if not check_if_channel_exists(channel_id):
                add_channel_info(channel_id, channel_name, channel_url)

        self.video_ids = list(set(video["video_id"] for video in playlist_data))

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.console.print(f"[green][bold]Downloading [red]{len(playlist_data)}[/red] "
                               "vtt files[/bold][/green]\n")
            self.tmp_dir = tmp_dir
            self.download_vtts()

    def update_channel(self, target_channel: str | int) -> None:

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.tmp_dir = tmp_dir
            
            # Handle both channel_id and target_channel (rowid/name)
            if isinstance(target_channel, str) and len(target_channel) == 24:  # YouTube channel IDs are 24 chars
                self.channel_id = target_channel
            else:
                self.channel_id = get_channel_id_from_input(target_channel)
                
            channel_url = f"https://www.youtube.com/channel/{self.channel_id}/videos"
            try:
                self.session = self.init_session(channel_url)
                self.channel_name = self.get_channel_name(self.channel_id)
            except requests.RequestException as e:
                raise click.ClickException(f"Could not get channel name: {e}") from e
            self.console.print(f"Updating channel: {self.channel_name}")
            public_video_ids = self.get_videos_list(channel_url)
            local_vid_ids = get_vid_ids_by_channel_id(self.channel_id)
            local_vid_ids = {i[0] for i in local_vid_ids}

            fresh_videos = [i for i in dict.fromkeys(public_video_ids) if i not in local_vid_ids]
            self.video_ids = fresh_videos

            self.console.print(f"Found {len(fresh_videos)} videos on \"{self.channel_name}\" not in the database")
            self.console.print(f"Downloading {len(fresh_videos)} new videos from \"{self.channel_name}\"")

            self.download_vtts()

            vtt_to_parse = os.listdir(self.tmp_dir)

            if len(vtt_to_parse) == 0:
                self.console.print("No new videos saved")
                self.console.print(f"{len(fresh_videos)} videos on \"{self.channel_name}\" do not have subtitles")
                return

            self.vtt_to_db()

            self.console.print(f"Added {len(vtt_to_parse)} new videos from \"{self.channel_name}\" to the database")

    def update_all_channels(self) -> None:

        self.console.print("Updating all channels in the database")
        all_channels = get_channels()
        all_channel_row_ids = [i[0] for i in all_channels]

        for channel_row_id in all_channel_row_ids:
            self.update_channel(channel_row_id)

        self.console.print("[green]Finished updating all channels[/green]")

    def init_session(self, url: str) -> requests.Session:
        s = requests.session()
        self._pace_request()
        handle_reject_consent_cookie(url, s)
        return s

    def get_channel_id(self, url: str) -> str:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                    'sleep_interval_requests': 1, 'retries': 0, 'extractor_retries': 0}
        if self.cookies_from_browser is not None:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)

        try:
            with self._youtube_dl(ydl_opts) as ydl:
                self._pace_request()
                info = ydl.extract_info(url, download=False, process=False)
                for _ in range(5):
                    if not isinstance(info, dict) or info.get('_type') not in ('url', 'url_transparent'):
                        break
                    redirect_url = info.get('url')
                    if not isinstance(redirect_url, str):
                        break
                    try:
                        redirect_url = self.validate_channel_url(redirect_url)
                    except ValueError:
                        break
                    self._pace_request()
                    info = ydl.extract_info(redirect_url, download=False, process=False)
        except Exception as e:
            self.console.print(f"[red]Error resolving channel URL: {e}[/red]")
            sys.exit(1)

        channel_id = info.get('channel_id') if isinstance(info, dict) else None
        if not isinstance(channel_id, str) or not re.fullmatch(r'UC[a-zA-Z0-9_-]{22}', channel_id):
            self.console.print("[red]Error: Could not resolve a valid YouTube channel ID from this URL[/red]")
            sys.exit(1)

        self.channel_name = (info.get('channel') or info.get('uploader')) if isinstance(info, dict) else None
        return channel_id

    def get_channel_name(self, channel_id: str) -> str:

        session = self.session
        self._pace_request()
        res = session.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=10)

        if res.status_code == 200:
            with self.console.status("[bold green]Parsing Feed...") as status:
                try:
                    tree = ElementTree.fromstring(res.content)
                except ElementTree.ParseError as e:
                    raise click.ClickException("YouTube returned an invalid channel feed") from e
                name = tree.find('./{*}author/{*}name')
                if name is None or not name.text:
                    raise click.ClickException("YouTube channel feed does not contain a channel name")
                return name.text
        else:
            self.console.print("[red]Error:[/red] "
                               "couldn't get the channel name or channel doesn't exist")
            sys.exit(1)

    def get_videos_list(self, channel_url: str) -> list[str]:
        with self.console.status("[bold green]Scraping video urls ...") as status:
            ydl_opts = {
                'http_headers': {
                    'User-Agent': random.choice(self._user_agents)
                },
                'extract_flat': True,
                'sleep_interval_requests': 1,
                'quiet': True,
                'sleep_interval': 1,
                'max_sleep_interval': 3,
                'retries': 0,
                'extractor_retries': 0,
            }

            if self.cookies_from_browser is not None:
                ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)

            try:
                with self._youtube_dl(ydl_opts) as ydl:
                    self._pace_request()
                    info = ydl.extract_info(channel_url, download=False)
                    if info and 'entries' in info:
                        list_of_videos_urls = [entry['id'] for entry in info['entries'] if entry]
                    else:
                        self.console.print("[red]Error: Could not extract video list from channel[/red]")
                        return []
            except Exception as e:
                error_msg = str(e)
                if "403" in error_msg or "Forbidden" in error_msg:
                    self.console.print("[red]403 Forbidden error when scraping channel videos[/red]")
                    self.console.print("[yellow]This might be due to:[/yellow]")
                    self.console.print("  - Channel is private or restricted")
                    self.console.print("  - YouTube is blocking automated access")
                    self.console.print("  - Missing cookies (try --cookies-from-browser)")
                    self.console.print("  - Rate limiting")
                else:
                    self.console.print(f"[red]Error scraping videos: {error_msg}[/red]")
                return []

            # Try to get streams as well
            streams_url = channel_url.replace("/videos", "/streams")
            try:
                with self._youtube_dl(ydl_opts) as ydl:
                    self._pace_request()
                    streams_info = ydl.extract_info(streams_url, download=False)
                    if streams_info and 'entries' in streams_info:
                        live_stream_urls = [entry['id'] for entry in streams_info['entries'] if entry]
                        if len(live_stream_urls) > 0:
                            list_of_videos_urls.extend(live_stream_urls)
            except Exception:
                self.console.print("[yellow]No streams found[/yellow]")

        return list_of_videos_urls

    def get_playlist_data(self, playlist_url: str) -> list[dict[str, str]]:
        with self.console.status("[bold green]Scraping video urls...") as status:
            ydl_opts = {
                'http_headers': {   
                    'User-Agent': random.choice(self._user_agents)
                },
                'quiet': True,
                'extract_flat': True,
                'sleep_interval_requests': 1,
                'retries': 0,
                'extractor_retries': 0,
            }

            if self.cookies_from_browser is not None:
                ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)
            with self._youtube_dl(ydl_opts) as ydl:
                self._pace_request()
                info = ydl.extract_info(playlist_url, download=False)
                playlist_data = []
                for entry in info['entries']:
                    vid_obj = {
                        'channel_name': entry['channel'],
                        'channel_id': entry['channel_id'],
                        'video_id': entry['id'],
                        'channel_url': f"https://www.youtube.com/channel/{entry['channel_id']}/videos",
                        'video_url': f"https://youtu.be/{entry['id']}"
                    }
                    playlist_data.append(vid_obj)

        return playlist_data

    def download_vtts(self) -> None:
        with ThreadPoolExecutor(self.number_of_jobs) as executor:
            futures = {executor.submit(self.get_vtt, self.tmp_dir, f'https://www.youtube.com/watch?v={video_id}', self.language): video_id for video_id in self.video_ids}
            for future in as_completed(futures):
                if future.result():
                    self.vtt_to_db({futures[future]})

    def quiet_progress_hook(self, d: dict) -> None:
        console = self.console
        if d['status'] == 'finished':
            file_name = Path(d['filename']).name
            console.print(f" -> \"{file_name}\"")

    def get_vtt(self, tmp_dir: str, video_url: str, language: str) -> bool:
        ydl_opts = {
            'outtmpl': f'{tmp_dir}/%(id)s',
            'writeinfojson': True,
            'writeautomaticsub': True,
            'subtitlesformat': 'vtt',
            'skip_download': True,
            'subtitleslangs': ['en', '-live_chat'],
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [self.quiet_progress_hook],
            'ignoreerrors': False,
            'sleep_interval_requests': 1,
            'sleep_interval_subtitles': 1,
            'retries': 0,
            'fragment_retries': 0,
            'extractor_retries': 0,
        }
        if self.cookies_from_browser is not None:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)

        for attempt in range(3):
            try:
                self._pace_request()
                with self._youtube_dl(ydl_opts) as ydl:
                    result = ydl.download([video_url])
                if result:
                    self.console.print(f"[red]Subtitle download failed for {video_url}[/red]")
                    return False
                return True
            except Exception as error:
                message = str(error)
                self.console.print(f"[yellow]Attempt {attempt + 1}/3 failed for {video_url}: {message}[/yellow]")
                if "429" not in message and "too many requests" not in message.lower():
                    return False
                self._rate_limit_cooldown(10 * (attempt + 1))
        return False

    def vtt_to_db(self, video_ids: set[str] | None = None) -> int:
        imported = 0
        with closing(sqlite3.connect(get_db_path())) as con:
            for vtt in sorted(Path(self.tmp_dir).glob('*.vtt')):
                vid_id = vtt.name.split('.')[0]
                if video_ids is not None and vid_id not in video_ids:
                    continue
                with open(vtt.with_name(f'{vid_id}.info.json'), encoding='utf-8') as info_file:
                    info = json.load(info_file)
                subtitles = parse_vtt(str(vtt))
                if not subtitles:
                    continue
                # One transaction per transcript: interruption leaves the video retryable.
                with con:
                    if con.execute("SELECT 1 FROM Subtitles WHERE video_id = ? LIMIT 1", (vid_id,)).fetchone():
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO Videos (video_id, video_title, video_url, video_date, channel_id) VALUES (?, ?, ?, ?, ?)",
                        (vid_id, info['title'], f"https://youtu.be/{vid_id}", get_date(info['upload_date']), info['channel_id']),
                    )
                    con.executemany(
                        "INSERT INTO Subtitles (video_id, start_time, stop_time, text) VALUES (?, ?, ?, ?)",
                        [(vid_id, sub['start_time'], sub['stop_time'], sub['text']) for sub in subtitles],
                    )
                imported += 1
        return imported

    def diagnose_403_errors(self, test_url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ") -> None:
        """
        !SLOP GENERATED

        Diagnose potential causes of 403 errors by testing various aspects of the connection
        """
        self.console.print("[bold blue]Diagnosing 403 errors...[/bold blue]")
        
        # Test 1: Basic HTTP request
        self.console.print("\n[bold]1. Testing basic HTTP connection to YouTube...[/bold]")
        try:
            import requests
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get("https://www.youtube.com", headers=headers, timeout=10)
            if response.status_code == 200:
                self.console.print("[green]✓ Basic HTTP connection successful[/green]")
            else:
                self.console.print(f"[red]✗ HTTP connection failed with status {response.status_code}[/red]")
        except Exception as e:
            self.console.print(f"[red]✗ HTTP connection failed: {e}[/red]")
        
        # Test 2: yt-dlp info extraction
        self.console.print("\n[bold]2. Testing yt-dlp info extraction...[/bold]")
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
            }
            
            if self.cookies_from_browser is not None:
                ydl_opts['cookiesfrombrowser'] = (self.cookies_from_browser,)
            
            with self._youtube_dl(ydl_opts) as ydl:
                info = ydl.extract_info(test_url, download=False)
                if info:
                    self.console.print("[green]✓ yt-dlp info extraction successful[/green]")
                    self.console.print(f"  Title: {info.get('title', 'N/A')}")
                else:
                    self.console.print("[red]✗ yt-dlp info extraction failed[/red]")
        except Exception as e:
            self.console.print(f"[red]✗ yt-dlp info extraction failed: {e}[/red]")
        
        # Test 3: Cookie availability
        self.console.print("\n[bold]3. Testing cookie availability...[/bold]")
        if self.cookies_from_browser is not None:
            self.console.print(f"[yellow]Cookies from browser: {self.cookies_from_browser}[/yellow]")
            try:
                # Test if we can extract cookies
                ydl_opts = {
                    'cookiesfrombrowser': (self.cookies_from_browser,),
                    'quiet': True,
                    'no_warnings': True,
                }
                with self._youtube_dl(ydl_opts) as ydl:
                    # Just test cookie extraction, don't download
                    self.console.print("[green]✓ Cookie extraction appears to be working[/green]")
            except Exception as e:
                self.console.print(f"[red]✗ Cookie extraction failed: {e}[/red]")
        else:
            self.console.print("[yellow]No cookies from browser specified[/yellow]")
            self.console.print("[yellow]Consider using --cookies-from-browser option[/yellow]")
        
        # Test 4: Rate limiting check
        self.console.print("\n[bold]4. Rate limiting analysis...[/bold]")
        self.console.print(f"Current parallel jobs: {self.number_of_jobs}")
        if self.number_of_jobs > 4:
            self.console.print("[yellow]⚠ High number of parallel jobs may cause rate limiting[/yellow]")
            self.console.print("[yellow]Consider reducing to 2-4 jobs[/yellow]")
        else:
            self.console.print("[green]✓ Parallel jobs setting looks reasonable[/green]")
        
        # Test 5: Network connectivity
        self.console.print("\n[bold]5. Network connectivity test...[/bold]")
        try:
            import socket
            socket.create_connection(("www.youtube.com", 443), timeout=5)
            self.console.print("[green]✓ Network connectivity to YouTube is good[/green]")
        except Exception as e:
            self.console.print(f"[red]✗ Network connectivity issue: {e}[/red]")
        
        # Recommendations
        self.console.print("\n[bold blue]Recommendations to fix 403 errors:[/bold blue]")
        self.console.print("1. Use --cookies-from-browser chrome (or firefox)")
        self.console.print("2. Reduce parallel jobs: -j 2 or -j 4")
        self.console.print("3. Wait a few minutes between download attempts")
        self.console.print("4. Check if you're behind a VPN or proxy")
        self.console.print("5. Try downloading from a different network")
        self.console.print("6. Update yt-dlp: pip install --upgrade yt-dlp")

    def validate_channel_url(self, channel_url: str) -> str:
        parsed = urlparse(channel_url.strip())
        supported_hosts = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in supported_hosts
                or parsed.port not in (None, 80, 443) or parsed.username or parsed.password):
            raise ValueError(f"Invalid YouTube channel URL: {channel_url}")

        parts = [part for part in parsed.path.split('/') if part]
        if len(parts) == 1 and parts[0].startswith('@') and len(parts[0]) > 1:
            channel_path = parts[0]
        elif len(parts) == 2 and parts[0] == 'channel' and re.fullmatch(r'UC[a-zA-Z0-9_-]{22}', parts[1]):
            channel_path = f'channel/{parts[1]}'
        elif len(parts) == 2 and parts[0].startswith('@') and len(parts[0]) > 1 and parts[1] in {'videos', 'streams', 'shorts', 'live', 'featured', 'playlists', 'channels', 'about'}:
            channel_path = parts[0]
        elif len(parts) == 3 and parts[0] == 'channel' and re.fullmatch(r'UC[a-zA-Z0-9_-]{22}', parts[1]) and parts[2] in {'videos', 'streams', 'shorts', 'live', 'featured', 'playlists', 'channels', 'about'}:
            channel_path = f'channel/{parts[1]}'
        else:
            raise ValueError(f"Invalid YouTube channel URL: {channel_url}")

        return f"https://www.youtube.com/{channel_path}/videos"


    @property
    def _user_agents(self):
        return [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"
        ]
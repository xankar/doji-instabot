"""
Discord bot that mirrors a friend's evening Instagram posts into a Discord channel.

The bot polls Instagram on a schedule, checks for new posts, and forwards any images
to the configured Discord channel. Carousel posts are handled by downloading each
image individually and uploading them as a set of Discord attachments.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Iterable, List, Optional

import discord
import httpx
import instaloader
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(exist_ok=True)


def parse_time(value: str, default: time) -> time:
    """Parse `HH:MM` formatted strings into `time` objects."""
    if not value:
        return default
    try:
        hour_str, minute_str = value.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
        return time(hour=hour, minute=minute)
    except Exception as exc:  # pragma: no cover - defensive parsing
        raise ValueError(f"Invalid time value '{value}'. Expected HH:MM format.") from exc


def read_env_time(name: str, fallback: time) -> time:
    return parse_time(os.getenv(name, "").strip(), fallback)


def read_env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return fallback
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def read_env_bool(name: str, fallback: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    discord_token: str
    instagram_username: str
    discord_channel_id: int
    check_interval_minutes: int
    evening_start: time
    evening_end: time
    timezone: ZoneInfo
    backfill_on_start: bool
    instagram_login_username: Optional[str]
    instagram_login_password: Optional[str]
    instagram_session_username: Optional[str]
    instagram_session_file: Optional[Path]

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise RuntimeError("Missing DISCORD_TOKEN in environment.")

        insta_username = os.getenv("INSTAGRAM_USERNAME")
        if not insta_username:
            raise RuntimeError("Missing INSTAGRAM_USERNAME in environment.")

        channel_id_raw = os.getenv("DISCORD_CHANNEL_ID")
        if not channel_id_raw:
            raise RuntimeError("Missing DISCORD_CHANNEL_ID in environment.")
        try:
            channel_id = int(channel_id_raw)
        except ValueError as exc:
            raise RuntimeError("DISCORD_CHANNEL_ID must be an integer.") from exc

        tz_name = os.getenv("TIMEZONE", "UTC")
        try:
            timezone = ZoneInfo(tz_name)
        except Exception as exc:
            raise RuntimeError(
                f"Invalid TIMEZONE '{tz_name}'. Use an IANA timezone string like 'America/New_York'."
            ) from exc

        interval = read_env_int("CHECK_INTERVAL_MINUTES", 20)
        evening_start = read_env_time("EVENING_START", time(17, 0))
        evening_end = read_env_time("EVENING_END", time(23, 59))

        backfill_on_start = read_env_bool("BACKFILL_ON_START", False)

        login_username = os.getenv("INSTAGRAM_LOGIN_USERNAME") or None
        login_password = os.getenv("INSTAGRAM_LOGIN_PASSWORD") or None
        if (login_username and not login_password) or (login_password and not login_username):
            raise RuntimeError(
                "INSTAGRAM_LOGIN_USERNAME and INSTAGRAM_LOGIN_PASSWORD must both be provided to enable authenticated access."
            )

        session_username = os.getenv("INSTAGRAM_SESSION_USERNAME") or None
        session_file_raw = os.getenv("INSTAGRAM_SESSION_FILE") or None
        session_file = Path(session_file_raw).expanduser() if session_file_raw else None
        if session_file and not session_username:
            raise RuntimeError(
                "Provide INSTAGRAM_SESSION_USERNAME alongside INSTAGRAM_SESSION_FILE so the session can be loaded."
            )

        return cls(
            discord_token=token,
            instagram_username=insta_username,
            discord_channel_id=channel_id,
            check_interval_minutes=interval,
            evening_start=evening_start,
            evening_end=evening_end,
            timezone=timezone,
            backfill_on_start=backfill_on_start,
            instagram_login_username=login_username,
            instagram_login_password=login_password,
            instagram_session_username=session_username,
            instagram_session_file=session_file,
        )


class BotState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_shortcode: Optional[str] = None

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            self.last_shortcode = payload.get("last_shortcode")
        except (json.JSONDecodeError, OSError) as exc:  # pragma: no cover
            raise RuntimeError(f"Failed to load state file: {self.path}") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fp:
            json.dump({"last_shortcode": self.last_shortcode}, fp, indent=2)


class InstagramFetcher:
    """
    Thin wrapper around Instaloader to fetch the latest post information without downloading files.
    """

    def __init__(
        self,
        username: str,
        login_username: Optional[str] = None,
        login_password: Optional[str] = None,
        session_username: Optional[str] = None,
        session_file: Optional[Path] = None,
    ) -> None:
        self.username = username
        # Instaloader emits a lot of logging; keep it quiet.
        self._loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
        )
        self._lock = asyncio.Lock()
        self._ensure_authenticated(login_username, login_password, session_username, session_file)

    def _ensure_authenticated(
        self,
        login_username: Optional[str],
        login_password: Optional[str],
        session_username: Optional[str],
        session_file: Optional[Path],
    ) -> None:
        if session_username:
            try:
                if session_file:
                    self._loader.load_session_from_file(session_username, str(session_file))
                else:
                    self._loader.load_session_from_file(session_username)
                # Session loaded successfully; no further action required.
                return
            except FileNotFoundError:
                location = str(session_file) if session_file else f"default session path for {session_username}"
                print(f"[instaloader] Session file not found at {location}; falling back to login.")
            except instaloader.exceptions.InstaloaderException as exc:
                print(f"[instaloader] Could not load stored session: {exc}. Falling back to login.")

        if login_username and login_password:
            try:
                self._loader.login(login_username, login_password)
                if session_username and not session_file:
                    # Persist session for reuse in default location.
                    self._loader.save_session_to_file()
            except instaloader.exceptions.TwoFactorAuthRequiredException as exc:
                raise RuntimeError(
                    "Instagram login requires two-factor authentication. "
                    "Run `python -m instaloader --login <username>` once to store a session, "
                    "then restart the bot."
                ) from exc
            except instaloader.exceptions.BadCredentialsException as exc:
                raise RuntimeError("Instagram login failed: verify username/password.") from exc
            except instaloader.exceptions.ConnectionException as exc:
                raise RuntimeError(f"Instagram login failed: {exc}") from exc


    async def fetch_new_posts(self, last_shortcode: Optional[str], max_posts: int = 3) -> List[instaloader.Post]:
        """
        Retrieve up to `max_posts` new posts newer than `last_shortcode`.
        The newest posts are returned first in chronological order (oldest → newest).
        """

        async with self._lock:
            return await asyncio.to_thread(self._fetch_new_posts_sync, last_shortcode, max_posts)

    def _fetch_new_posts_sync(
        self, last_shortcode: Optional[str], max_posts: int = 3
    ) -> List[instaloader.Post]:
        context = self._loader.context
        profile = instaloader.Profile.from_username(context, self.username)
        collected: List[instaloader.Post] = []

        for post in profile.get_posts():
            if last_shortcode and post.shortcode == last_shortcode:
                break
            collected.append(post)
            if len(collected) >= max_posts:
                break

        collected.reverse()
        return collected


async def download_media(urls: Iterable[str]) -> List[discord.File]:
    """
    Download the given media URLs and wrap them as Discord `File` objects.
    """
    files: List[discord.File] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for idx, url in enumerate(urls, start=1):
            response = await client.get(url)
            response.raise_for_status()
            suffix = "mp4" if url.endswith(".mp4") else "jpg"
            filename = f"instagram_{idx}.{suffix}"
            buffer = io.BytesIO(response.content)
            buffer.seek(0)
            files.append(discord.File(fp=buffer, filename=filename))
    return files


class InstaMirrorBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents(guilds=True, messages=True, message_content=False)
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.state = BotState(STATE_FILE)
        self.fetcher = InstagramFetcher(
            settings.instagram_username,
            login_username=settings.instagram_login_username,
            login_password=settings.instagram_login_password,
            session_username=settings.instagram_session_username,
            session_file=settings.instagram_session_file,
        )
        self.target_channel: Optional[discord.TextChannel] = None

    async def setup_hook(self) -> None:
        _ensure_data_dir()
        self.state.load()
        self.target_channel = self.get_channel(self.settings.discord_channel_id)  # type: ignore[assignment]
        if self.target_channel is None:
            # If cache miss, fetch explicitly once ready.
            self.target_channel = await self.fetch_channel(self.settings.discord_channel_id)  # type: ignore[assignment]
        self.instagram_poll.start()

    async def on_ready(self) -> None:
        channel_info = f"{self.target_channel.guild.name}#{self.target_channel.name}" if self.target_channel else "unknown"
        print(f"Logged in as {self.user} | Mirroring to {channel_info}")

    def within_evening_window(self, now: datetime) -> bool:
        start = self.settings.evening_start
        end = self.settings.evening_end

        if start <= end:
            return start <= now.time() <= end
        # Handle wrap-around windows (e.g., 21:00 - 02:00)
        return now.time() >= start or now.time() <= end

    @tasks.loop(minutes=1)
    async def instagram_poll(self) -> None:
        now = datetime.now(self.settings.timezone)
        if not self.within_evening_window(now):
            return

        if now.minute % self.settings.check_interval_minutes != 0:
            return

        if not self.target_channel:
            print("Target channel unavailable; will retry next poll.")
            return

        try:
            await self._process_new_posts()
        except Exception as exc:
            print(f"[instagram_poll] Error while processing: {exc}")

    async def _process_new_posts(self) -> None:
        last_shortcode = self.state.last_shortcode

        if last_shortcode is None and not self.settings.backfill_on_start:
            latest = await self.fetcher.fetch_new_posts(last_shortcode=None, max_posts=1)
            if latest:
                self.state.last_shortcode = latest[-1].shortcode
                self.state.save()
                print(
                    "State seeded with most recent Instagram post; "
                    "set BACKFILL_ON_START=1 to mirror immediately."
                )
            return

        max_posts = 1 if last_shortcode is None else 3
        posts = await self.fetcher.fetch_new_posts(last_shortcode=last_shortcode, max_posts=max_posts)
        if not posts:
            return

        for post in posts:
            await self._relay_post(post)
            self.state.last_shortcode = post.shortcode
            self.state.save()

    async def _relay_post(self, post: instaloader.Post) -> None:
        if not self.target_channel:
            raise RuntimeError("Discord channel not ready.")

        caption = post.caption or ""
        permalink = f"https://www.instagram.com/p/{post.shortcode}/"
        timestamp = post.date_utc.strftime("%Y-%m-%d %H:%M UTC")

        media_urls: List[str]
        if post.typename == "GraphSidecar":
            media_urls = [node.video_url or node.display_url for node in post.get_sidecar_nodes()]
        elif post.is_video and post.video_url:
            media_urls = [post.video_url]
        else:
            media_urls = [post.url]

        files = await download_media(media_urls)

        content_lines = [
            f"New Instagram post by **{self.settings.instagram_username}**",
            permalink,
            f"Posted: {timestamp}",
        ]
        if caption:
            content_lines.append("")
            content_lines.append(caption.strip())

        message = await self.target_channel.send(content="\n".join(content_lines), files=files)

        # Suppress Discord's automatic Instagram embed; we already relay the content above.
        if message and permalink:
            try:
                await message.edit(suppress=True)
            except discord.HTTPException as exc:
                print(f"[discord] Failed to suppress embed: {exc}")


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    bot = InstaMirrorBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()

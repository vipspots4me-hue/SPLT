import os
import sys
import time
import json
import shutil
import socket
import logging
import subprocess
import threading
import urllib.request
import urllib.error
import zipfile
import tarfile
import fcntl
from pathlib import Path

import streamlit as st


# ============================================================
# SPMA - FULL SELF-CONTAINED STREAMLIT VERSION
# ============================================================

BASE_DIR = Path("/mount/src/spma")

BIN_DIR = BASE_DIR / ".bin"
VENV_DIR = BASE_DIR / ".spotdl_venv"

DOWNLOAD_DIR = BASE_DIR / "downloads"
CACHE_DIR = BASE_DIR / ".cache"
BGUTIL_DIR = BASE_DIR / ".bgutil-ytdlp-pot-provider"

BGUTIL_SOURCE_DIR = BASE_DIR / ".bgutil-source"

DENO_BIN = BIN_DIR / "deno"
FFMPEG_BIN = BIN_DIR / "ffmpeg"
FFPROBE_BIN = BIN_DIR / "ffprobe"

PYTHON_BIN = VENV_DIR / "bin" / "python"
SPOTDL_BIN = VENV_DIR / "bin" / "spotdl"

BGUTIL_SERVER = BGUTIL_DIR / "server"

BGUTIL_PORT = 4416
WARP_PORT = 40000

WARP_PROXY = f"socks5h://127.0.0.1:{WARP_PORT}"


# ============================================================
# FIXED VERSIONS
# ============================================================

SPOTDL_VERSION = "4.4.11"
YTDLP_VERSION = "2026.08.19"
YTDLP_EJS_VERSION = "0.8.0"
BGUTIL_VERSION = "2.0.0"
DENO_VERSION = "2.9.6"
FFMPEG_VERSION = "7.0.2"

SPOTDL_GIT = (
    "git+https://github.com/TzurSoffer/spotify-downloader@"
    "29cb0b0669d5c107331b0912fdef73967b47493e"
)

BGUTIL_GIT = (
    "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
)


# ============================================================
# URLS
# ============================================================

DENO_URL = (
    f"https://dl.deno.land/release/v{DENO_VERSION}/"
    "deno-x86_64-unknown-linux-gnu.zip"
)

FFMPEG_URL = (
    f"https://www.johnvansickle.com/ffmpeg/releases/"
    f"ffmpeg-{FFMPEG_VERSION}-amd64-static.tar.xz"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("SPMA")


# ============================================================
# DIRECTORIES
# ============================================================

for directory in [
    BASE_DIR,
    BIN_DIR,
    DOWNLOAD_DIR,
    CACHE_DIR,
    BGUTIL_DIR,
]:
    directory.mkdir(parents=True, exist_ok=True)


# ============================================================
# COMMAND RUNNER
# ============================================================

def run_command(
    command,
    timeout=None,
    env=None,
    cwd=None,
    check=False,
):
    """
    Run command safely and log output.
    """

    if isinstance(command, (list, tuple)):
        display_command = " ".join(str(x) for x in command)
    else:
        display_command = str(command)

    logger.info("Running: %s", display_command)

    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )

        output = result.stdout or ""

        if output.strip():
            logger.info(output.strip())

        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): "
                f"{display_command}\n{output}"
            )

        return result.returncode, output

    except Exception as exc:
        logger.exception("Command failed: %s", exc)

        if check:
            raise

        return -1, str(exc)


# ============================================================
# ENVIRONMENT / PROXY
# ============================================================

def clear_proxy_environment():
    """
    Remove all proxy variables.

    This is important because a dead SOCKS proxy at
    127.0.0.1:40000 can break pip, git, urllib and curl.
    """

    proxy_keys = [
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
    ]

    for key in proxy_keys:
        os.environ.pop(key, None)


def warp_proxy_available():
    """
    Check whether WARP SOCKS5 is really listening.
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        sock.settimeout(2)
        sock.connect(("127.0.0.1", WARP_PORT))
        return True

    except Exception:
        return False

    finally:
        sock.close()


def configure_warp_proxy():
    """
    Enable WARP proxy ONLY when the local SOCKS listener exists.

    Returns:
        True  -> WARP available
        False -> direct network mode
    """

    clear_proxy_environment()

    if not warp_proxy_available():
        logger.warning(
            "WARP SOCKS5 is NOT available on "
            "127.0.0.1:%s. Using direct network connection.",
            WARP_PORT,
        )
        return False

    os.environ["ALL_PROXY"] = WARP_PROXY
    os.environ["all_proxy"] = WARP_PROXY

    os.environ["HTTP_PROXY"] = WARP_PROXY
    os.environ["http_proxy"] = WARP_PROXY

    os.environ["HTTPS_PROXY"] = WARP_PROXY
    os.environ["https_proxy"] = WARP_PROXY

    os.environ["NO_PROXY"] = (
        "127.0.0.1,"
        "localhost,"
        "0.0.0.0"
    )

    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    logger.info(
        "WARP SOCKS5 detected on 127.0.0.1:%s",
        WARP_PORT,
    )

    return True


def check_warp():
    """
    Check WARP through curl.
    """

    if not warp_proxy_available():
        return False

    command = [
        "curl",
        "--socks5-hostname",
        f"127.0.0.1:{WARP_PORT}",
        "--connect-timeout",
        "10",
        "-fsSL",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]

    code, output = run_command(
        command,
        timeout=20,
    )

    if code != 0:
        logger.warning("WARP trace test failed.")
        return False

    if "warp=on" in output:
        logger.info("WARP test: OK")
        return True

    logger.warning(
        "WARP SOCKS is reachable, but Cloudflare trace does not "
        "report warp=on."
    )

    return False


# ============================================================
# CURL DOWNLOADER
# ============================================================

def curl_download(url, destination, timeout=300):
    """
    Download a file using curl.

    If WARP is enabled in environment, curl uses it.
    Otherwise it uses direct connection.

    We intentionally do NOT force a dead proxy here.
    """

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temp_file = destination.with_suffix(
        destination.suffix + ".part"
    )

    try:
        temp_file.unlink(missing_ok=True)
    except Exception:
        pass

    command = [
        "curl",
        "-L",
        "--fail",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        "-o",
        str(temp_file),
        url,
    ]

    code, output = run_command(
        command,
        timeout=timeout + 60,
    )

    if code != 0:
        logger.error(
            "Download failed: %s",
            url,
        )
        return False

    if not temp_file.exists():
        logger.error(
            "Downloaded file does not exist: %s",
            temp_file,
        )
        return False

    if temp_file.stat().st_size == 0:
        logger.error(
            "Downloaded file is empty: %s",
            temp_file,
        )
        return False

    temp_file.replace(destination)

    logger.info(
        "Download complete: %s",
        destination,
    )

    return True


# ============================================================
# DENO
# ============================================================

def install_deno():
    """
    Install Deno into:
        /mount/src/spma/.bin/deno
    """

    if DENO_BIN.exists():
        code, output = run_command(
            [str(DENO_BIN), "--version"],
            timeout=20,
        )

        if code == 0:
            logger.info(
                "Deno already installed: %s",
                output.strip().splitlines()[0]
                if output.strip()
                else "unknown",
            )
            return True

        try:
            DENO_BIN.unlink()
        except Exception:
            pass

    logger.info(
        "Installing Deno %s...",
        DENO_VERSION,
    )

    zip_path = BASE_DIR / ".deno.zip"

    if not curl_download(
        DENO_URL,
        zip_path,
        timeout=300,
    ):
        logger.error("Deno installation FAILED.")
        return False

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(BIN_DIR)

        if not DENO_BIN.exists():
            logger.error(
                "Deno binary was not found after extraction."
            )
            return False

        DENO_BIN.chmod(0o755)

        code, output = run_command(
            [str(DENO_BIN), "--version"],
            timeout=20,
        )

        if code != 0:
            logger.error("Deno test failed.")
            return False

        logger.info(
            "Deno %s: OK",
            DENO_VERSION,
        )

        return True

    except Exception:
        logger.exception("Deno extraction failed.")
        return False

    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# FFMPEG
# ============================================================

def install_ffmpeg():
    """
    Install static FFmpeg and ffprobe into .bin.
    """

    if FFMPEG_BIN.exists() and FFPROBE_BIN.exists():

        code, output = run_command(
            [str(FFMPEG_BIN), "-version"],
            timeout=20,
        )

        if code == 0:
            logger.info("FFmpeg already installed.")
            return True

    logger.info(
        "Installing FFmpeg %s...",
        FFMPEG_VERSION,
    )

    archive_path = BASE_DIR / ".ffmpeg.tar.xz"

    if not curl_download(
        FFMPEG_URL,
        archive_path,
        timeout=300,
    ):
        logger.error("FFmpeg installation FAILED.")
        return False

    extract_dir = BASE_DIR / ".ffmpeg_extract"

    try:
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        extract_dir.mkdir(parents=True)

        with tarfile.open(
            archive_path,
            mode="r:xz",
        ) as archive:
            archive.extractall(extract_dir)

        ffmpeg_found = None
        ffprobe_found = None

        for path in extract_dir.rglob("ffmpeg"):
            if path.is_file():
                ffmpeg_found = path
                break

        for path in extract_dir.rglob("ffprobe"):
            if path.is_file():
                ffprobe_found = path
                break

        if not ffmpeg_found:
            logger.error(
                "ffmpeg binary was not found."
            )
            return False

        if not ffprobe_found:
            logger.error(
                "ffprobe binary was not found."
            )
            return False

        shutil.copy2(
            ffmpeg_found,
            FFMPEG_BIN,
        )

        shutil.copy2(
            ffprobe_found,
            FFPROBE_BIN,
        )

        FFMPEG_BIN.chmod(0o755)
        FFPROBE_BIN.chmod(0o755)

        code, output = run_command(
            [str(FFMPEG_BIN), "-version"],
            timeout=20,
        )

        if code != 0:
            logger.error(
                "FFmpeg verification failed."
            )
            return False

        logger.info(
            "FFmpeg %s: OK",
            FFMPEG_VERSION,
        )

        return True

    except Exception:
        logger.exception(
            "FFmpeg extraction failed."
        )
        return False

    finally:
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:
            pass

        try:
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
        except Exception:
            pass


# ============================================================
# VIRTUAL ENVIRONMENT
# ============================================================

def ensure_venv():
    if PYTHON_BIN.exists():
        return True

    logger.info(
        "Creating spotDL virtual environment..."
    )

    code, output = run_command(
        [
            sys.executable,
            "-m",
            "venv",
            str(VENV_DIR),
        ],
        timeout=180,
    )

    if code != 0:
        logger.error(
            "Virtual environment creation failed."
        )
        return False

    return PYTHON_BIN.exists()


# ============================================================
# PIP
# ============================================================

def pip_install(*packages):
    if not PYTHON_BIN.exists():
        return False

    command = [
        str(PYTHON_BIN),
        "-m",
        "pip",
        "install",
        "--upgrade",
        *packages,
    ]

    code, output = run_command(
        command,
        timeout=600,
    )

    return code == 0


def upgrade_pip():
    logger.info(
        "Updating pip/wheel/setuptools..."
    )

    return pip_install(
        "pip",
        "wheel",
        "setuptools",
    )


# ============================================================
# SPOTDL
# ============================================================

def install_spotdl():

    logger.info(
        "Installing spotDL %s...",
        SPOTDL_VERSION,
    )

    code, output = run_command(
        [
            str(PYTHON_BIN),
            "-m",
            "pip",
            "install",
            "--upgrade",
            SPOTDL_GIT,
        ],
        timeout=900,
    )

    if code != 0:
        logger.error(
            "spotDL installation failed."
        )
        return False

    code, output = run_command(
        [
            str(SPOTDL_BIN),
            "--version",
        ],
        timeout=30,
    )

    if code != 0:
        logger.error(
            "spotDL verification failed."
        )
        return False

    version = output.strip()

    logger.info(
        "spotDL %s: OK",
        version,
    )

    return True


# ============================================================
# YT-DLP
# ============================================================

def install_ytdlp():

    logger.info(
        "Installing yt-dlp %s...",
        YTDLP_VERSION,
    )

    if not pip_install(
        f"yt-dlp=={YTDLP_VERSION}"
    ):
        return False

    logger.info(
        "Installing yt-dlp-ejs %s...",
        YTDLP_EJS_VERSION,
    )

    if not pip_install(
        f"yt-dlp-ejs=={YTDLP_EJS_VERSION}"
    ):
        return False

    code, output = run_command(
        [
            str(PYTHON_BIN),
            "-m",
            "yt_dlp",
            "--version",
        ],
        timeout=30,
    )

    if code != 0:
        logger.error(
            "yt-dlp verification failed."
        )
        return False

    logger.info(
        "yt-dlp: %s",
        output.strip(),
    )

    return True


# ============================================================
# BGUTIL PYTHON PLUGIN
# ============================================================

def install_bgutil_plugin():

    logger.info(
        "Installing bgutil plugin %s...",
        BGUTIL_VERSION,
    )

    return pip_install(
        f"bgutil-ytdlp-pot-provider=={BGUTIL_VERSION}"
    )


# ============================================================
# BGUTIL SOURCE
# ============================================================

def clone_bgutil_source():

    if BGUTIL_SOURCE_DIR.exists():

        if (
            (BGUTIL_SOURCE_DIR / ".git").exists()
        ):
            logger.info(
                "bgutil source already exists."
            )

            code, output = run_command(
                [
                    "git",
                    "-C",
                    str(BGUTIL_SOURCE_DIR),
                    "pull",
                    "--ff-only",
                ],
                timeout=300,
            )

            return code == 0

        try:
            shutil.rmtree(BGUTIL_SOURCE_DIR)
        except Exception:
            pass

    logger.info(
        "Cloning bgutil source..."
    )

    code, output = run_command(
        [
            "git",
            "clone",
            "--depth",
            "1",
            BGUTIL_GIT,
            str(BGUTIL_SOURCE_DIR),
        ],
        timeout=600,
    )

    if code != 0:
        logger.error(
            "bgutil source clone failed."
        )
        return False

    return True


# ============================================================
# BGUTIL SERVER
# ============================================================

def find_bgutil_server():

    candidates = [
        BGUTIL_SOURCE_DIR / "server" / "server.ts",
        BGUTIL_SOURCE_DIR / "server.ts",
        BGUTIL_SOURCE_DIR / "src" / "server.ts",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def start_bgutil_server():

    if not DENO_BIN.exists():
        logger.error(
            "Cannot start bgutil: Deno unavailable."
        )
        return False

    if not install_bgutil_plugin():
        logger.error(
            "bgutil Python dependency failed."
        )
        return False

    if not clone_bgutil_source():
        return False

    # Check whether server is already listening.
    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    )

    try:
        sock.settimeout(1)
        sock.connect(
            ("127.0.0.1", BGUTIL_PORT)
        )

        logger.info(
            "bgutil server already running."
        )

        return True

    except Exception:
        pass

    finally:
        sock.close()

    server_ts = find_bgutil_server()

    if server_ts is None:
        logger.error(
            "Could not find bgutil server.ts."
        )
        return False

    logger.info(
        "Installing bgutil Deno dependencies..."
    )

    code, output = run_command(
        [
            str(DENO_BIN),
            "install",
            "--allow-scripts=npm:canvas",
            "--frozen",
        ],
        cwd=BGUTIL_SOURCE_DIR,
        timeout=900,
    )

    if code != 0:
        logger.error(
            "bgutil dependency installation failed."
        )
        return False

    env = os.environ.copy()

    env["PATH"] = (
        str(BIN_DIR)
        + os.pathsep
        + env.get("PATH", "")
    )

    env["BGUTIL_PORT"] = str(BGUTIL_PORT)

    logger.info(
        "Starting bgutil server on port %s...",
        BGUTIL_PORT,
    )

    log_file = BASE_DIR / "bgutil-server.log"

    try:
        log_handle = open(
            log_file,
            "a",
            encoding="utf-8",
        )

        process = subprocess.Popen(
            [
                str(DENO_BIN),
                "run",
                "--allow-net",
                "--allow-read",
                "--allow-env",
                "--allow-run",
                str(server_ts),
            ],
            cwd=str(BGUTIL_SOURCE_DIR),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        logger.info(
            "bgutil process started: PID %s",
            process.pid,
        )

    except Exception:
        logger.exception(
            "Could not start bgutil server."
        )
        return False

    # Wait for server.
    for _ in range(30):

        time.sleep(1)

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        try:
            sock.settimeout(1)
            sock.connect(
                ("127.0.0.1", BGUTIL_PORT)
            )

            logger.info(
                "bgutil server: OK"
            )

            return True

        except Exception:
            pass

        finally:
            sock.close()

    logger.error(
        "bgutil server did not start."
    )

    return False


# ============================================================
# PATH ENVIRONMENT
# ============================================================

def configure_environment():

    bin_path = str(BIN_DIR)

    current_path = os.environ.get(
        "PATH",
        "",
    )

    if bin_path not in current_path.split(
        os.pathsep
    ):
        os.environ["PATH"] = (
            bin_path
            + os.pathsep
            + current_path
        )

    os.environ["FFMPEG_BINARY"] = str(
        FFMPEG_BIN
    )

    os.environ["FFPROBE_BINARY"] = str(
        FFPROBE_BIN
    )

    logger.info(
        "Environment configured."
    )


# ============================================================
# YOUTUBE TEST
# ============================================================

def youtube_test():

    if not (
        PYTHON_BIN.exists()
        and DENO_BIN.exists()
        and FFMPEG_BIN.exists()
    ):
        logger.warning(
            "YouTube tests skipped because "
            "required dependencies are not ready."
        )
        return False

    test_url = (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )

    logger.info(
        "Testing YouTube audio extraction..."
    )

    command = [
        str(PYTHON_BIN),
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--skip-download",
        "--format",
        "251",
        "--extractor-args",
        "youtube:player_client=mweb;fetch_pot=always",
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url="
        f"http://127.0.0.1:{BGUTIL_PORT}",
        test_url,
    ]

    code, output = run_command(
        command,
        timeout=120,
    )

    if code == 0:
        logger.info(
            "YouTube test: OK"
        )
        return True

    logger.warning(
        "YouTube mweb test failed."
    )

    # Fallback test
    command = [
        str(PYTHON_BIN),
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--skip-download",
        "--format",
        "251",
        "--extractor-args",
        "youtube:player_client=tv",
        test_url,
    ]

    code, output = run_command(
        command,
        timeout=120,
    )

    if code == 0:
        logger.info(
            "YouTube TV fallback: OK"
        )
        return True

    logger.warning(
        "YouTube test failed."
    )

    return False


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_LOCK_FILE = Path(
    "/tmp/spma_telegram_bot.lock"
)

telegram_lock_handle = None


def start_telegram_bot():

    global telegram_lock_handle

    try:
        from telegram import Update
        from telegram.ext import (
            Updater,
            CommandHandler,
            MessageHandler,
            Filters,
            CallbackContext,
        )
    except Exception:
        logger.exception(
            "python-telegram-bot is not installed."
        )
        return False

    try:
        token = st.secrets.get(
            "TELEGRAM_BOT_TOKEN",
            "",
        )
    except Exception:
        token = ""

    if not token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN is not configured."
        )
        return False

    try:
        telegram_lock_handle = open(
            TELEGRAM_LOCK_FILE,
            "w",
        )

        fcntl.flock(
            telegram_lock_handle,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

    except BlockingIOError:
        logger.warning(
            "Telegram bot already running."
        )
        return True

    except Exception:
        logger.exception(
            "Telegram lock error."
        )
        return False

    def start(update, context):
        update.message.reply_text(
            "SPMA bot is online."
        )

    def text_message(update, context):
        text = (
            update.message.text
            if update.message
            else ""
        )

        if not text:
            return

        update.message.reply_text(
            "Received: " + text
        )

    try:
        updater = Updater(
            token=token,
            use_context=True,
        )

        dispatcher = updater.dispatcher

        dispatcher.add_handler(
            CommandHandler(
                "start",
                start,
            )
        )

        dispatcher.add_handler(
            MessageHandler(
                Filters.text & ~Filters.command,
                text_message,
            )
        )

        thread = threading.Thread(
            target=updater.start_polling,
            kwargs={
                "drop_pending_updates": True,
            },
            daemon=True,
            name="spma-telegram",
        )

        thread.start()

        logger.info(
            "Telegram bot started."
        )

        return True

    except Exception:
        logger.exception(
            "Telegram bot failed."
        )
        return False


# ============================================================
# DOWNLOAD SONG
# ============================================================

def cleanup_output_files():

    if not DOWNLOAD_DIR.exists():
        return

    for item in DOWNLOAD_DIR.iterdir():

        if item.is_file():
            try:
                item.unlink()
            except Exception:
                pass


def download_song(url):

    if not SPOTDL_BIN.exists():
        return False, "spotDL is not installed."

    if not FFMPEG_BIN.exists():
        return False, "FFmpeg is not installed."

    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # METHOD 1 - MWEB + BGUTIL
    # --------------------------------------------------------

    cleanup_output_files()

    logger.info(
        "Downloading with YouTube mweb + bgutil..."
    )

    command = [
        str(SPOTDL_BIN),
        url,
        "--output",
        str(DOWNLOAD_DIR / "{artist} - {title}.{output-ext}"),
        "--format",
        "mp3",
        "--bitrate",
        "320k",
        "--threads",
        "4",
        "--no-cache",
        "--overwrite",
        "force",
        "--yt-dlp-args",
        (
            "--extractor-args "
            "'youtube:player_client=mweb;fetch_pot=always'"
            " "
            "--extractor-args "
            f"'youtubepot-bgutilhttp:base_url="
            f"http://127.0.0.1:{BGUTIL_PORT}'"
        ),
    ]

    code, output = run_command(
        command,
        timeout=900,
    )

    files = list(
        DOWNLOAD_DIR.glob("*.mp3")
    )

    if code == 0 and files:
        return True, str(files[0])

    logger.warning(
        "mweb download failed. Trying TV fallback."
    )

    # --------------------------------------------------------
    # METHOD 2 - TV CLIENT
    # --------------------------------------------------------

    cleanup_output_files()

    command = [
        str(SPOTDL_BIN),
        url,
        "--output",
        str(DOWNLOAD_DIR / "{artist} - {title}.{output-ext}"),
        "--format",
        "mp3",
        "--bitrate",
        "320k",
        "--threads",
        "4",
        "--no-cache",
        "--overwrite",
        "force",
        "--yt-dlp-args",
        (
            "--extractor-args "
            "'youtube:player_client=tv'"
        ),
    ]

    code, output = run_command(
        command,
        timeout=900,
    )

    files = list(
        DOWNLOAD_DIR.glob("*.mp3")
    )

    if code == 0 and files:
        return True, str(files[0])

    return False, output


# ============================================================
# INITIALIZATION
# ============================================================

@st.cache_resource(
    show_spinner=False
)
def initialize_spma():

    logger.info(
        "========================================"
    )

    logger.info(
        "SPMA initialization started"
    )

    logger.info(
        "========================================"
    )

    # --------------------------------------------------------
    # IMPORTANT:
    # Never configure a dead WARP proxy.
    # --------------------------------------------------------

    warp_ok = configure_warp_proxy()

    if warp_ok:
        warp_test = check_warp()
    else:
        warp_test = False

        logger.warning(
            "WARP unavailable. "
            "Continuing with direct network."
        )

    # --------------------------------------------------------
    # DENO
    # --------------------------------------------------------

    deno_ok = install_deno()

    # --------------------------------------------------------
    # FFMPEG
    # --------------------------------------------------------

    ffmpeg_ok = install_ffmpeg()

    # --------------------------------------------------------
    # VENV
    # --------------------------------------------------------

    venv_ok = ensure_venv()

    if not venv_ok:
        logger.error(
            "Virtual environment unavailable."
        )

        return {
            "warp": warp_test,
            "deno": deno_ok,
            "ffmpeg": ffmpeg_ok,
            "spotdl": False,
            "ytdlp": False,
            "bgutil_plugin": False,
            "bgutil_source": False,
            "bgutil_server": False,
        }

    # --------------------------------------------------------
    # PIP
    # --------------------------------------------------------

    pip_ok = upgrade_pip()

    # --------------------------------------------------------
    # SPOTDL
    # --------------------------------------------------------

    spotdl_ok = install_spotdl()

    # --------------------------------------------------------
    # YT-DLP
    # --------------------------------------------------------

    ytdlp_ok = install_ytdlp()

    # --------------------------------------------------------
    # BGUTIL
    # --------------------------------------------------------

    bgutil_plugin_ok = False
    bgutil_source_ok = False
    bgutil_server_ok = False

    if (
        deno_ok
        and spotdl_ok
        and ytdlp_ok
    ):

        bgutil_plugin_ok = (
            install_bgutil_plugin()
        )

        if bgutil_plugin_ok:
            bgutil_source_ok = (
                clone_bgutil_source()
            )

        if bgutil_source_ok:
            bgutil_server_ok = (
                start_bgutil_server()
            )

    else:

        logger.error(
            "Skipping bgutil source because "
            "Deno or spotDL is unavailable."
        )

    # --------------------------------------------------------
    # ENVIRONMENT
    # --------------------------------------------------------

    configure_environment()

    # --------------------------------------------------------
    # VERSION CHECK
    # --------------------------------------------------------

    if ytdlp_ok:

        code, output = run_command(
            [
                str(PYTHON_BIN),
                "-m",
                "yt_dlp",
                "--version",
            ],
            timeout=30,
        )

        if code == 0:
            logger.info(
                "yt-dlp: %s",
                output.strip(),
            )

    # --------------------------------------------------------
    # YOUTUBE TEST
    # --------------------------------------------------------

    if (
        bgutil_server_ok
        and deno_ok
        and ffmpeg_ok
        and ytdlp_ok
    ):
        youtube_test()

    else:
        logger.warning(
            "YouTube tests skipped because "
            "bgutil/yt-dlp/Deno/FFmpeg "
            "is not ready."
        )

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

    start_telegram_bot()

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    logger.info(
        "========================================"
    )

    logger.info(
        "SPMA initialization finished"
    )

    logger.info(
        "Deno = %s",
        "OK" if deno_ok else "FAILED",
    )

    logger.info(
        "FFmpeg = %s",
        "OK" if ffmpeg_ok else "FAILED",
    )

    logger.info(
        "spotDL = %s",
        "OK" if spotdl_ok else "FAILED",
    )

    logger.info(
        "yt-dlp = %s",
        "OK" if ytdlp_ok else "FAILED",
    )

    logger.info(
        "bgutil dependencies = %s",
        "OK" if bgutil_plugin_ok else "FAILED",
    )

    logger.info(
        "bgutil source = %s",
        "OK" if bgutil_source_ok else "FAILED",
    )

    logger.info(
        "bgutil server = %s",
        "OK" if bgutil_server_ok else "FAILED",
    )

    logger.info(
        "WARP = %s",
        "OK" if warp_test else "FAILED",
    )

    logger.info(
        "========================================"
    )

    return {
        "warp": warp_test,
        "deno": deno_ok,
        "ffmpeg": ffmpeg_ok,
        "spotdl": spotdl_ok,
        "ytdlp": ytdlp_ok,
        "bgutil_plugin": bgutil_plugin_ok,
        "bgutil_source": bgutil_source_ok,
        "bgutil_server": bgutil_server_ok,
    }


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="SPMA",
    page_icon="🎵",
    layout="centered",
)


st.title("🎵 SPMA")

st.caption(
    "Spotify / YouTube Music Downloader"
)


# Initialize only once per Streamlit process.
status = initialize_spma()


# ============================================================
# STATUS
# ============================================================

st.subheader("System status")

status_items = [
    ("Deno", status["deno"]),
    ("FFmpeg", status["ffmpeg"]),
    ("spotDL", status["spotdl"]),
    ("yt-dlp", status["ytdlp"]),
    ("bgutil plugin", status["bgutil_plugin"]),
    ("bgutil source", status["bgutil_source"]),
    ("bgutil server", status["bgutil_server"]),
    ("WARP", status["warp"]),
]


for name, ok in status_items:

    if ok:
        st.success(
            f"{name}: OK",
            icon="✅",
        )
    else:
        st.warning(
            f"{name}: FAILED",
            icon="⚠️",
        )


# ============================================================
# DOWNLOAD UI
# ============================================================

st.divider()

st.subheader("Download")

url = st.text_input(
    "Spotify / YouTube URL",
    placeholder="Paste URL here...",
)


if st.button(
    "Download MP3 320K",
    type="primary",
    use_container_width=True,
):

    if not url.strip():

        st.error(
            "Please enter a URL."
        )

    elif not status["spotdl"]:

        st.error(
            "spotDL is not ready."
        )

    elif not status["ffmpeg"]:

        st.error(
            "FFmpeg is not ready."
        )

    else:

        with st.spinner(
            "Downloading..."
        ):

            success, result = download_song(
                url.strip()
            )

        if success:

            output_file = Path(result)

            st.success(
                "Download completed."
            )

            if output_file.exists():

                with open(
                    output_file,
                    "rb",
                ) as file:

                    st.download_button(
                        label="⬇️ Download MP3",
                        data=file,
                        file_name=output_file.name,
                        mime="audio/mpeg",
                        use_container_width=True,
                    )

        else:

            st.error(
                "Download failed."
            )

            if result:
                st.code(
                    result[-5000:]
                )

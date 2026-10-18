"""URL helpers for Twilio webhooks."""

import logging
import os
import shutil
import subprocess
import threading
import time

import httpx

logger = logging.getLogger(__name__)


def normalize_public_url(url: str) -> str:
    return url.strip().rstrip("/")


def public_https_to_wss(url: str) -> str:
    """Convert PUBLIC_URL https → wss for Twilio Media Streams."""
    base = normalize_public_url(url)
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    if base.startswith(("wss://", "ws://")):
        return base
    return f"wss://{base}"


def get_local_ngrok_tunnel() -> dict | None:
    """Return the active local HTTPS ngrok tunnel config, if any."""
    try:
        resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=2.0)
        resp.raise_for_status()
        for tunnel in resp.json().get("tunnels", []):
            if tunnel.get("proto") == "https" and tunnel.get("public_url"):
                return tunnel
    except Exception as exc:
        logger.debug("ngrok auto-detect failed: %s", exc)
    return None


def discover_ngrok_url() -> str:
    """Read HTTPS tunnel URL from a local ngrok agent (port 4040)."""
    tunnel = get_local_ngrok_tunnel()
    if tunnel:
        return normalize_public_url(tunnel["public_url"])
    return ""


def _ngrok_agent_up() -> bool:
    try:
        resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5)
        return resp.status_code == 200
    except Exception:
        return False


def _ngrok_executable() -> str:
    configured = os.environ.get("NGROK_EXE", "").strip()
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which("ngrok")
    if found:
        return found
    winget = (
        r"C:\Users\ADMIN\AppData\Local\Microsoft\WinGet\Packages"
        r"\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
    )
    if os.path.isfile(winget):
        return winget
    return ""


_ngrok_spawn_lock = threading.Lock()


def _ngrok_process_running() -> bool:
    """True when an ngrok agent is already open. A second one is rejected."""
    try:
        listing = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq ngrok.exe", "/NH"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return "ngrok.exe" in listing.lower()


def _spawn_ngrok(exe: str, port: int) -> None:
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    subprocess.Popen(
        [exe, "http", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=os.name != "nt",
    )


def _open_tunnel_on_agent(port: int) -> str:
    try:
        resp = httpx.post(
            "http://127.0.0.1:4040/api/tunnels",
            json={"addr": str(port), "proto": "http", "name": "voice-bot"},
            timeout=5.0,
        )
        if resp.status_code < 400:
            url = str((resp.json() or {}).get("public_url") or "")
            if url.startswith("https://"):
                return normalize_public_url(url)
    except Exception as exc:
        logger.info("ngrok tunnel create failed: %s", exc)
    return discover_ngrok_url()


def ensure_ngrok_tunnel(port: int = 8000, wait_s: float = 12.0) -> str:
    """Return the live https tunnel. Never start a second ngrok agent."""
    found = discover_ngrok_url()
    if found:
        return found
    with _ngrok_spawn_lock:
        found = discover_ngrok_url()
        if found:
            return found
        already_running = _ngrok_process_running() or _ngrok_agent_up()
        if already_running:
            created = _open_tunnel_on_agent(port) if _ngrok_agent_up() else ""
            if created:
                return created
        else:
            exe = _ngrok_executable()
            if not exe:
                raise RuntimeError(
                    "ngrok is not installed. Install it with: winget install Ngrok.Ngrok"
                )
            logger.info("Starting one ngrok tunnel to port %s", port)
            _spawn_ngrok(exe, port)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            found = discover_ngrok_url()
            if found:
                return found
            time.sleep(0.4)
    if _ngrok_process_running():
        raise RuntimeError(
            "ngrok is already running for this address. "
            "Close the extra ngrok window. The free account allows only 3 agents, "
            "and this https address is already online."
        )
    raise RuntimeError("ngrok started, but no https address appeared yet.")


def validate_public_url_for_twilio(public_url: str) -> str:
    """Ensure PUBLIC_URL is an absolute URL Twilio can reach."""
    url = normalize_public_url(public_url)

    if not url or not url.startswith(("http://", "https://")):
        raise ValueError(
            "PUBLIC_URL is missing or invalid in backend/.env. "
            "Twilio needs a full public URL, e.g. "
            "PUBLIC_URL=https://your-subdomain.ngrok-free.app"
        )

    lower = url.lower()
    if "localhost" in lower or "127.0.0.1" in lower or lower.startswith("http://192.168."):
        raise ValueError(
            "PUBLIC_URL cannot be localhost or a private IP — Twilio cannot reach your machine. "
            "Run: ngrok http 8000  then set PUBLIC_URL to the https ngrok URL in backend/.env "
            "and restart the backend."
        )

    return url


def is_public_url_ready(public_url: str) -> bool:
    try:
        validate_public_url_for_twilio(public_url)
        return True
    except ValueError:
        return False


def resolve_public_url(env_url: str) -> tuple[str, str]:
    """Return (effective_url, source) where source is env, ngrok, or none."""
    if env_url and is_public_url_ready(env_url):
        return normalize_public_url(env_url), "env"

    discovered = discover_ngrok_url()
    if discovered and is_public_url_ready(discovered):
        return discovered, "ngrok"

    return normalize_public_url(env_url), "none"


def verify_public_url_reachable(public_url: str) -> tuple[bool, str]:
    """
    Check that Twilio can reach the webhook URL.

    For local dev, uses the ngrok agent API (port 4040) — never HTTP back to this
    server (that deadlocks uvicorn's single worker). For production, pings /health/ping.
    """
    url = normalize_public_url(public_url)
    if not is_public_url_ready(url):
        return False, (
            "PUBLIC_URL is missing or invalid. Run ngrok http 8000 and set the https URL "
            "in backend/.env or use Auto-detect in the dashboard."
        )

    tunnel = get_local_ngrok_tunnel()
    if tunnel:
        tunnel_url = normalize_public_url(tunnel["public_url"])
        addr = str(tunnel.get("config", {}).get("addr", ""))
        if tunnel_url == url:
            if "8000" in addr:
                return True, "OK"
            return False, (
                f"ngrok is running but points to {addr}, not port 8000. "
                "Run: ngrok http --domain=YOUR-DOMAIN 8000"
            )
        return False, (
            f"ngrok is running at {tunnel_url} but PUBLIC_URL is {url}. "
            "Click Auto-detect in the dashboard or update backend/.env."
        )

    health_url = f"{url}/api/health/ping"
    try:
        resp = httpx.get(
            health_url,
            timeout=5.0,
            follow_redirects=True,
            headers={"ngrok-skip-browser-warning": "true", "User-Agent": "VoiceCallAI-HealthCheck"},
        )
    except httpx.ConnectError:
        return False, (
            f"Cannot reach {url} — your ngrok tunnel is probably stopped or the URL expired. "
            "Run: ngrok http 8000 then click Auto-detect ngrok in the dashboard and try again."
        )
    except httpx.TimeoutException:
        return False, f"Timed out reaching {url}. Check ngrok is running and PUBLIC_URL is current."

    content_type = resp.headers.get("content-type", "")
    body_lower = resp.text[:500].lower()

    if resp.status_code == 404 and "ngrok" in body_lower:
        return False, (
            "This ngrok URL is expired or offline (tunnel not running). "
            "Start ngrok: ngrok http 8000 — then Auto-detect ngrok in the dashboard."
        )

    if "text/html" in content_type and "ngrok" in body_lower:
        return False, (
            "ngrok returned a browser warning page instead of your app. "
            "Restart ngrok with a fresh URL, save it via Auto-detect, and restart the backend."
        )

    if resp.status_code != 200:
        return False, (
            f"Webhook URL returned HTTP {resp.status_code}. "
            "Ensure ngrok points to port 8000 and the backend is running."
        )

    try:
        data = resp.json()
    except ValueError:
        return False, (
            "Webhook URL did not return JSON from /api/health/ping. "
            "Twilio will play 'application error' — fix PUBLIC_URL / ngrok first."
        )

    if data.get("status") != "ok":
        return False, "Backend health check failed at the public URL."

    return True, "OK"


def ensure_reachable_public_url(env_url: str) -> tuple[str, str]:
    """
    Return (url, source) that Twilio can actually reach.
    Prefers live ngrok over a stale PUBLIC_URL in .env.
    """
    candidates: list[tuple[str, str]] = []

    if env_url and is_public_url_ready(env_url):
        candidates.append((normalize_public_url(env_url), "env"))

    discovered = discover_ngrok_url()
    if discovered and is_public_url_ready(discovered):
        if not any(u == discovered for u, _ in candidates):
            candidates.insert(0, (discovered, "ngrok"))

    for url, source in candidates:
        ok, _ = verify_public_url_reachable(url)
        if ok:
            return url, source

    if candidates:
        _, msg = verify_public_url_reachable(candidates[0][0])
        raise ValueError(msg)

    raise ValueError(
        "No reachable PUBLIC_URL. Run ngrok http 8000, click Auto-detect in the dashboard, "
        "then restart the backend."
    )

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import socketio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


if TYPE_CHECKING:
    import uvicorn

    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins="*", logger=False, engineio_logger=False
)

# Wrap with ASGI app
socket_app = socketio.ASGIApp(sio, app)

# Global references (set by main.py)
gui_manager: WebGUIManager | None = None
twitch_client: Twitch | None = None
_server_instance: uvicorn.Server | None = None


def set_managers(gui: WebGUIManager, twitch: Twitch):
    """Called by main.py to set up references"""
    global gui_manager, twitch_client
    gui_manager = gui
    twitch_client = twitch
    gui.set_socketio(sio)


# Pydantic models for API
class LoginRequest(BaseModel):
    username: str
    password: str
    token: str = ""


class ChannelSelectRequest(BaseModel):
    channel_id: int


class SettingsUpdate(BaseModel):
    games_to_watch: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    priority_list_only: bool | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    inventory_filters: dict | None = None
    inventory_list_view: bool | None = None
    mining_benefits: dict[str, bool] | None = None
    telegram_enabled: bool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_panel_url: str | None = None
    telegram_notifications: dict[str, bool] | None = None
    free_games_enabled: bool | None = None
    free_games_runner: str | None = None
    free_games_image: str | None = None
    free_games_claimer_path: str | None = None
    free_games_schedule_hours: int | None = None
    free_games_run_timeout_minutes: int | None = None
    free_games_accounts: list[dict] | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


class FreeGamesRunRequest(BaseModel):
    account_id: str | None = None
    interactive: bool = True
    exclusive: bool = True


class HubActionRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


# ==================== REST API Endpoints ====================


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the main web interface"""
    # Web files are in project_root/web/, we're in project_root/src/web/
    web_dir = Path(__file__).parent.parent.parent / "web"
    index_file = web_dir / "index.html"
    logger.debug(
        f"Looking for web files: __file__={__file__}, web_dir={web_dir}, index_file={index_file}, exists={index_file.exists()}"
    )
    if index_file.exists():
        return FileResponse(index_file)
    return HTMLResponse(
        content=f"<h1>Twitch Drops Miner</h1><p>Web interface files not found. Please check installation.</p><p>Debug: Looking for {index_file}</p>",
        status_code=500,
    )


@app.get("/api/status")
async def get_status():
    """Get current application status"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {
        "status": gui_manager.status.get(),
        "login": gui_manager.login.get_status(),
        "manual_mode": twitch_client.get_manual_mode_info(),
    }


@app.get("/api/channels")
async def get_channels():
    """Get list of tracked channels"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"channels": gui_manager.channels.get_channels()}


@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest):
    """Select a channel to watch"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Validate channel exists
    channel = twitch_client.channels.get(request.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate channel has a game
    if not channel.game:
        raise HTTPException(status_code=400, detail="Channel is not playing any game")

    # Warn if channel has no drops (shouldn't happen if GUI is filtering correctly)
    if not any(campaign.can_earn(channel) for campaign in twitch_client.inventory):
        logger.warning(f"User selected channel {channel.name} but it has no available drops")

    gui_manager.select_channel(request.channel_id)

    # Trigger channel switch to apply the selection
    from src.config import State

    twitch_client.change_state(State.CHANNEL_SWITCH)

    return {"success": True}


@app.get("/api/campaigns")
async def get_campaigns():
    """Get campaign inventory"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"campaigns": gui_manager.inv.get_campaigns()}


@app.get("/api/console")
async def get_console_history():
    """Get console output history"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"lines": gui_manager.output.get_history()}


@app.get("/api/settings")
async def get_settings():
    """Get current settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_settings()


@app.get("/api/languages")
async def get_languages():
    """Get available languages"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    """Get translations for current language"""
    from src.i18n.translator import _

    # Return the full Translation object
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update application settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    settings_dict = settings.dict(exclude_unset=True)
    gui_manager.settings.update_settings(settings_dict)
    return {"success": True, "settings": gui_manager.settings.get_settings()}


@app.post("/api/telegram/resend-status")
async def resend_telegram_status():
    """Resend the Telegram status message as a fresh message."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    success = await twitch_client.telegram.resend_status_message()
    if not success:
        raise HTTPException(status_code=400, detail="Telegram bot is not enabled or configured")
    return {"success": True}


@app.get("/api/free-games/status")
async def get_free_games_status():
    """Get free-games module status."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    return twitch_client.free_games.get_status()


@app.get("/api/free-games/logs/{kind}")
async def get_free_games_log(
    kind: str,
    account_id: str | None = None,
    max_chars: int = 8000,
):
    """Get a sanitized free-games module log tail."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    result = twitch_client.free_games.get_log(
        kind,
        account_id=account_id,
        max_chars=max_chars,
    )
    if not result.get("available"):
        raise HTTPException(status_code=404, detail="Log is not available")
    return result


@app.get("/api/hub/modules")
async def get_hub_modules():
    """Get normalized hub module catalog."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    return twitch_client.hub.get_status()


@app.post("/api/free-games/run")
async def run_free_games(request: FreeGamesRunRequest):
    """Run the Epic free-games claimer now."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    action = "run_account" if request.account_id else "run"
    result = twitch_client.hub.run_action(
        "free-games-epic",
        action,
        {
            "account_id": request.account_id,
            "interactive": request.interactive,
            "exclusive": request.exclusive,
        }
        if request.account_id
        else {"interactive": request.interactive, "exclusive": request.exclusive},
    )
    return _hub_action_response(result)


@app.post("/api/free-games/update")
async def update_free_games_runner():
    """Update the configured free-games runner."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    return _hub_action_response(twitch_client.hub.run_action("free-games-epic", "update"))


@app.post("/api/hub/modules/{module_id}/actions/{action}")
async def run_hub_module_action(module_id: str, action: str, request: HubActionRequest):
    """Run a normalized action for a hub module."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    return _hub_action_response(twitch_client.hub.run_action(module_id, action, request.params))


@app.post("/api/hub/actions/{action}")
async def run_hub_action(action: str):
    """Run a hub-level action."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    return _hub_action_response(twitch_client.hub.run_hub_action(action))


def _hub_action_response(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("success"):
        raise HTTPException(
            status_code=int(result.get("status_code") or 400),
            detail=str(result.get("detail") or "Hub action failed"),
        )
    return result


@app.api_route(
    "/api/free-games/vnc/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_free_games_vnc(path: str, request: Request):
    """Proxy the active Epic claimer noVNC UI through the hub panel."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")
    target = twitch_client.free_games.get_vnc_target_url(path, request.url.query)
    if not target:
        raise HTTPException(status_code=404, detail="Epic browser is not active")

    headers = _proxy_request_headers(request.headers)
    body = await request.body()
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60, sock_read=30))
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=headers,
            data=body if body else None,
            allow_redirects=False,
        )
    except Exception:
        await session.close()
        raise

    response_headers = _proxy_response_headers(upstream.headers)
    if location := response_headers.get("location"):
        response_headers["location"] = _rewrite_vnc_location(location)

    async def stream_response():
        try:
            async for chunk in upstream.content.iter_chunked(65536):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(
        stream_response(),
        status_code=upstream.status,
        headers=response_headers,
        media_type=upstream.content_type,
    )


@app.websocket("/api/free-games/vnc/{path:path}")
async def proxy_free_games_vnc_websocket(websocket: WebSocket, path: str):
    """Proxy the active Epic claimer noVNC websocket."""
    if not twitch_client:
        await websocket.close(code=1011)
        return
    target = twitch_client.free_games.get_vnc_target_url(
        path,
        websocket.url.query,
        websocket=True,
    )
    if not target:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(target) as upstream:
                await _relay_websocket(websocket, upstream)
        except Exception:
            logger.warning("Epic noVNC websocket proxy failed", exc_info=True)
            await websocket.close(code=1011)


def _proxy_request_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _proxy_response_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    }


def _rewrite_vnc_location(location: str) -> str:
    if location.startswith("/"):
        return f"/api/free-games/vnc{location}"
    return location


async def _relay_websocket(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> None:
    client_to_upstream = asyncio.create_task(_relay_client_to_upstream(websocket, upstream))
    upstream_to_client = asyncio.create_task(_relay_upstream_to_client(websocket, upstream))
    done, pending = await asyncio.wait(
        {client_to_upstream, upstream_to_client},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


async def _relay_client_to_upstream(
    websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                await upstream.close()
                return
            if "bytes" in message and message["bytes"] is not None:
                await upstream.send_bytes(message["bytes"])
            elif "text" in message and message["text"] is not None:
                await upstream.send_str(message["text"])
    except WebSocketDisconnect:
        await upstream.close()


async def _relay_upstream_to_client(
    websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse
) -> None:
    async for message in upstream:
        if message.type == aiohttp.WSMsgType.TEXT:
            await websocket.send_text(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            await websocket.close()
            return


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest):
    """Verify proxy connectivity"""
    import time

    import aiohttp

    proxy_url = request.proxy.strip()
    if not proxy_url:
        return {"success": False, "message": "Proxy URL is empty"}

    try:
        start_time = time.time()
        # Test connection to Twitch
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://www.twitch.tv", proxy=proxy_url, timeout=10) as response,
        ):
            # Just checking if we can connect and get a response
            if response.status < 500:
                latency = round((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "message": f"Connected! ({latency}ms)",
                    "latency": latency,
                }
            else:
                return {
                    "success": False,
                    "message": f"Proxy reachable but returned {response.status}",
                }
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


@app.get("/api/version")
async def get_version():
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None

    try:
        # Check GitHub API for latest release
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://api.github.com/repos/rangermix/TwitchDropsMiner/releases/latest", timeout=5
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                latest_version = data.get("tag_name", "").lstrip("v")
                download_url = data.get("html_url")

                # Compare versions (simple string comparison works for semantic versioning)
                if latest_version and latest_version > current_version:
                    update_available = True
    except Exception as e:
        logger.warning(f"Failed to check for updates: {str(e)}")

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": download_url or "https://github.com/rangermix/TwitchDropsMiner/releases",
    }


@app.post("/api/login")
async def submit_login(login_data: LoginRequest):
    """Submit login credentials"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    gui_manager.login.submit_login(login_data.username, login_data.password, login_data.token)
    return {"success": True}


@app.post("/api/oauth/confirm")
async def confirm_oauth():
    """Confirm OAuth code has been entered by user"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Just set the event to signal the user has acknowledged the code
    gui_manager.login._login_event.set()
    return {"success": True}


@app.post("/api/reload")
async def trigger_reload():
    """Trigger application reload"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    from src.config import State

    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual_mode():
    """Exit manual mode and return to automatic channel selection"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    if not twitch_client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}

    twitch_client.exit_manual_mode("User requested")
    return {"success": True}


# ==================== Socket.IO Events ====================


@sio.event
async def connect(sid, environ):
    """Client connected"""
    logger.info(f"Web client connected: {sid}")

    # Send initial state to new client
    if gui_manager and twitch_client:
        await sio.emit(
            "initial_state",
            {
                "status": gui_manager.status.get(),
                "channels": gui_manager.channels.get_channels(),
                "campaigns": gui_manager.inv.get_campaigns(),
                "console": gui_manager.output.get_history(),
                "settings": gui_manager.settings.get_settings(),
                "login": gui_manager.login.get_status(),
                "manual_mode": twitch_client.get_manual_mode_info(),
                "current_drop": gui_manager.progress.get_current_drop(),
                "wanted_items": gui_manager.get_wanted_game_tree(),
                "free_games": twitch_client.free_games.get_status(),
                "hub": twitch_client.hub.get_status(),
            },
            room=sid,
        )


@sio.event
async def disconnect(sid):
    """Client disconnected"""
    logger.info(f"Web client disconnected: {sid}")


@sio.event
async def request_login(sid):
    """Client requested login form submission"""
    logger.info(f"Login request from client: {sid}")
    # The actual login data comes via REST API


@sio.event
async def request_reload(sid):
    """Client requested application reload"""
    if twitch_client:
        from src.config import State

        twitch_client.change_state(State.INVENTORY_FETCH)


@sio.event
async def get_wanted_items(sid):
    """Client requested wanted items list"""
    if gui_manager:
        await sio.emit("wanted_items_update", gui_manager.get_wanted_game_tree(), to=sid)


# Mount static files (CSS, JS, images)
# Web files are in project_root/web/, we're in project_root/src/web/
web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Development server runner
async def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (used for development/testing)"""
    global _server_instance
    import uvicorn

    config = uvicorn.Config(socket_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    _server_instance = server
    try:
        await server.serve()
    finally:
        _server_instance = None


async def shutdown_server():
    """Gracefully shutdown the web server"""
    if _server_instance:
        logger.info("Setting server.should_exit = True")
        _server_instance.should_exit = True
        # Give the server a moment to process the shutdown signal
        # The uvicorn server checks should_exit periodically
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    # For standalone testing
    asyncio.run(run_server())

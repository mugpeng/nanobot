"""FastAPI web server for nanobot."""

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from uvicorn import Config, Server

from nanobot import __logo__, __version__
from nanobot.config.loader import load_config
from nanobot.config.paths import get_cron_dir
from nanobot.cron.service import CronService


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""

    message: str
    session_id: str = "web:default"


app = FastAPI(
    title="nanobot",
    description="Personal AI Assistant",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global agent instance (singleton)
_agent = None
_agent_lock = asyncio.Lock()


async def get_agent():
    """Get or create the global agent instance."""
    global _agent
    if _agent is None:
        async with _agent_lock:
            # Double-check after acquiring lock
            if _agent is None:
                from nanobot.agent.loop import AgentLoop
                from nanobot.bus.queue import MessageBus
                from nanobot.session.manager import SessionManager

                from nanobot.cli.commands import _make_provider

                config = load_config()

                bus = MessageBus()
                provider = _make_provider(config)

                cron_store_path = get_cron_dir() / "jobs.json"
                cron = CronService(cron_store_path)

                session_manager = SessionManager(config.workspace_path)

                _agent = AgentLoop(
                    bus=bus,
                    provider=provider,
                    workspace=config.workspace_path,
                    model=config.agents.defaults.model,
                    max_iterations=config.agents.defaults.max_tool_iterations,
                    context_window_tokens=config.agents.defaults.context_window_tokens,
                    web_search_config=config.tools.web.search,
                    web_proxy=config.tools.web.proxy or None,
                    exec_config=config.tools.exec,
                    cron_service=cron,
                    restrict_to_workspace=config.tools.restrict_to_workspace,
                    session_manager=session_manager,
                    mcp_servers=config.tools.mcp_servers,
                    channels_config=config.channels,
                )

                # Connect MCP servers
                await _agent._connect_mcp()

    return _agent


async def _stream_response(message: str, session_id: str) -> AsyncIterator[str]:
    """Stream agent response via SSE."""
    agent = await get_agent()

    # Split session_id into channel and chat_id for web
    if ":" not in session_id:
        channel, chat_id = "web", session_id
    else:
        channel, chat_id = session_id.split(":", 1)

    # Create progress queue
    progress_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _progress(content: str, *, tool_hint: bool = False) -> None:
        """Queue progress updates."""
        event_type = "tool" if tool_hint else "progress"
        await progress_queue.put(
            f"event: {event_type}\ndata: {json.dumps({'content': content})}\n\n"
        )

    # Run the agent in background
    response_task = asyncio.create_task(
        agent.process_direct(
            message,
            session_key=session_id,
            channel=channel,
            chat_id=chat_id,
            on_progress=_progress,
        )
    )

    # Stream progress events as they come
    done = False
    while not done:
        try:
            item = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
            yield item
        except asyncio.TimeoutError:
            if response_task.done():
                done = True

    # Get final response
    response = await response_task
    yield f"event: done\ndata: {json.dumps({'content': response})}\n\n"


@app.post("/api/chat")
async def chat(request: ChatRequest):
    """Chat endpoint with SSE streaming."""
    return StreamingResponse(
        _stream_response(request.message, request.session_id),
        media_type="text/event-stream",
    )


@app.get("/api/sessions")
async def list_sessions():
    """List all sessions."""
    agent = await get_agent()
    sessions = agent.sessions.list_sessions()
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session history."""
    agent = await get_agent()
    session = agent.sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}
    return {"session": session.to_dict()}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    agent = await get_agent()
    if agent.sessions.get(session_id):
        agent.sessions.delete(session_id)
        agent.sessions.invalidate(session_id)
        return {"deleted": True}
    return {"deleted": False}


@app.get("/api/status")
async def get_status():
    """Get nanobot status."""
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()

    return {
        "version": __version__,
        "logo": __logo__,
        "config_path": str(config_path),
        "workspace": str(config.workspace_path),
        "model": config.agents.defaults.model,
    }


def run_server(host: str = "127.0.0.1", port: int = 8000, log_level: str = "info"):
    """Run the uvicorn server."""
    # Mount static files
    static_dir = Path(__file__).parent / "static"
    if not static_dir.exists():
        static_dir.mkdir(parents=True, exist_ok=True)

    # Mount static files - must be before returning from function
    # but after API routes are defined (FastAPI handles this correctly)
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    config = Config(app=app, host=host, port=port, log_level=log_level)
    server = Server(config)

    print(f"{__logo__} Starting web server on http://{host}:{port}")
    print(f"  Session ID prefix: web:")

    asyncio.run(server.serve())

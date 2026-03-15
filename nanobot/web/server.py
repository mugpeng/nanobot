"""Web interface for nanobot."""

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nanobot.agent.loop import AgentLoop
from nanobot.config.schema import Config
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _make_provider

# Global state
app = FastAPI(title="nanobot Web UI")
_agent_loop: AgentLoop | None = None
_bus: MessageBus | None = None

class ChatRequest(BaseModel):
    message: str
    session_id: str = "web:default"

def init_agent(config: Config) -> None:
    """Initialize the global agent loop for the web server."""
    global _agent_loop, _bus
    if _agent_loop is not None:
        return

    _bus = MessageBus()
    provider = _make_provider(config)
    
    _agent_loop = AgentLoop(
        bus=_bus,
        provider=provider,
        workspace=Path(config.agents.defaults.workspace).expanduser(),
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
    )

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    """Handle chat requests and stream the response via SSE."""
    if not _agent_loop:
        return {"error": "Agent not initialized"}

    async def event_generator() -> AsyncGenerator[str, None]:
        # Queue for progress updates
        progress_queue = asyncio.Queue()
        
        async def on_progress(msg: str, tool_hint: bool = False) -> None:
            event_type = "tool" if tool_hint else "thought"
            await progress_queue.put(f"event: {event_type}\ndata: {json.dumps(msg)}\n\n")

        # Run the agent in a background task so we can stream progress
        async def run_agent():
            try:
                response = await _agent_loop.process_direct(
                    content=request.message,
                    session_key=request.session_id,
                    channel="web",
                    chat_id=request.session_id,
                    on_progress=on_progress,
                )
                await progress_queue.put(f"event: message\ndata: {json.dumps(response)}\n\n")
            except Exception as e:
                await progress_queue.put(f"event: error\ndata: {json.dumps(str(e))}\n\n")
            finally:
                await progress_queue.put("event: done\ndata: {}\n\n")

        task = asyncio.create_task(run_agent())

        # Yield events as they come in
        while True:
            event = await progress_queue.get()
            yield event
            if event.startswith("event: done"):
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/sessions")
async def list_sessions():
    """List all available sessions."""
    if not _agent_loop:
        return {"sessions": []}
    
    sessions = []
    for key, session in _agent_loop.sessions._cache.items():
        if key.startswith("web:"):
            sessions.append({
                "id": key,
                "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                "message_count": len(session.messages)
            })
    return {"sessions": sorted(sessions, key=lambda x: x["updated_at"] or "", reverse=True)}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Clear a session."""
    if not _agent_loop:
        return {"success": False}
    
    session = _agent_loop.sessions.get(session_id)
    session.clear()
    _agent_loop.sessions.save(session)
    _agent_loop.sessions.invalidate(session_id)
    return {"success": True}

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

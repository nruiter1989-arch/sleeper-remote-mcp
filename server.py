"""
Sleeper Remote MCP Server
=========================

A small, dependency-light remote MCP server that wraps Sleeper's public
fantasy-football API (https://docs.sleeper.app/) over the Streamable HTTP
transport, so it can be added to Claude as a custom connector.

Why this exists: Sleeper rosters distinguish "reserve" (IR) players from
bench players in the raw API, but several third-party wrappers collapse
that distinction into a flat bench list. This server passes the `reserve`
(IR) and `taxi` arrays through untouched and enriched with player names,
so "who's actually on IR right now" is a direct, reliable read.

Adapted loosely from the tool surface of sourknives/sleeper-mcp-server
(MIT licensed), rewritten from scratch here to run over HTTP instead of
stdio, using only packages already vendored with the `mcp` SDK (httpx).

Auth: optional shared-secret bearer token. If SLEEPER_MCP_TOKEN is set in
the environment, every request must include `Authorization: Bearer <token>`.
If unset, the server is open (fine given all underlying data is already
public on Sleeper's own API, but the token is recommended once deployed
publicly to keep it from being casually scraped by others).
"""

import os
import time
import logging
from typing import Any, Optional

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.requests import Request

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sleeper-remote-mcp")

SLEEPER_BASE = "https://api.sleeper.app/v1"
AUTH_TOKEN = os.environ.get("SLEEPER_MCP_TOKEN")  # optional shared secret

mcp = FastMCP(
    name="sleeper",
    instructions=(
        "Tools for querying Sleeper fantasy football data: leagues, rosters "
        "(with true reserve/IR and taxi squad lists, not collapsed into "
        "'bench'), users, matchups, drafts, players, and NFL state."
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
)

_http = httpx.AsyncClient(base_url=SLEEPER_BASE, timeout=20.0)

# ---------------------------------------------------------------------------
# tiny in-memory TTL cache
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}


async def cached(key: str, ttl: int, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = await fn()
    _cache[key] = (now + ttl, value)
    return value


async def _get(path: str) -> Any:
    resp = await _http.get(path)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def _players_map() -> dict:
    async def fetch():
        data = await _get("/players/nfl")
        out = {}
        for pid, p in (data or {}).items():
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            full = (p.get("full_name") or f"{first} {last}".strip() or pid)
            out[pid] = {
                "full_name": full,
                "position": p.get("position"),
                "team": p.get("team"),
                "status": p.get("injury_status") or p.get("status"),
            }
        return out

    # Sleeper's player catalog is large and static intra-day; cache 24h.
    return await cached("players_map", 24 * 3600, fetch)


def _enrich(player_ids: Optional[list[str]], player_map: dict) -> list[dict]:
    out = []
    for pid in player_ids or []:
        info = player_map.get(pid, {"full_name": f"Player {pid}", "position": None, "team": None, "status": None})
        out.append({"player_id": pid, **info})
    return out


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_nfl_state() -> dict:
    """Get the current NFL state: season, week, and season type."""
    return await cached("nfl_state", 300, lambda: _get("/state/nfl"))


@mcp.tool()
async def get_user_leagues(username: str, season: str) -> dict:
    """Get all Sleeper NFL leagues for a username in a given season.

    Args:
        username: Sleeper username to look up.
        season: Season year, e.g. "2026".
    """
    user = await _get(f"/user/{username}")
    if not user:
        return {"error": f"User '{username}' not found"}
    leagues = await _get(f"/user/{user['user_id']}/leagues/nfl/{season}")
    return {
        "username": username,
        "user_id": user["user_id"],
        "season": season,
        "leagues": [
            {
                "league_id": lg["league_id"],
                "name": lg["name"],
                "status": lg.get("status"),
                "total_rosters": lg.get("total_rosters"),
                "sport": lg.get("sport"),
                "season_type": lg.get("season_type"),
            }
            for lg in (leagues or [])
        ],
    }


@mcp.tool()
async def get_league_info(league_id: str) -> dict:
    """Get settings and metadata for a specific Sleeper league."""
    league = await cached(f"league:{league_id}", 3600, lambda: _get(f"/league/{league_id}"))
    if not league:
        return {"error": f"League '{league_id}' not found"}
    return league


@mcp.tool()
async def get_league_rosters(league_id: str) -> dict:
    """Get every roster in a league, with players/starters/reserve/taxi lists.

    The 'reserve' list is Sleeper's real IR slot (players stashed on IR),
    kept separate from 'starters' and the rest of 'players' (bench) -- do
    not treat bench players as IR, and don't rely on any other field to
    infer IR status.
    """
    rosters_task = _get(f"/league/{league_id}/rosters")
    users_task = _get(f"/league/{league_id}/users")
    rosters, users = await rosters_task, await users_task
    if not rosters:
        return {"error": f"No rosters found for league '{league_id}'"}

    player_map = await _players_map()
    user_map = {u["user_id"]: {"username": u.get("username"), "display_name": u.get("display_name")} for u in (users or [])}

    out = []
    for r in rosters:
        players = r.get("players") or []
        starters = r.get("starters") or []
        reserve = r.get("reserve") or []
        taxi = r.get("taxi") or []
        bench = [p for p in players if p not in starters and p not in reserve and p not in taxi]
        out.append(
            {
                "roster_id": r.get("roster_id"),
                "owner_id": r.get("owner_id"),
                "owner_info": user_map.get(r.get("owner_id"), {"username": None, "display_name": f"User {r.get('owner_id')}"}),
                "starters": _enrich(starters, player_map),
                "bench": _enrich(bench, player_map),
                "reserve": _enrich(reserve, player_map),
                "taxi": _enrich(taxi, player_map),
                "settings": r.get("settings") or {},
            }
        )
    return {"league_id": league_id, "rosters": out}


@mcp.tool()
async def get_league_users(league_id: str) -> dict:
    """Get all users/managers in a league."""
    users = await _get(f"/league/{league_id}/users")
    if not users:
        return {"error": f"No users found for league '{league_id}'"}
    return {"league_id": league_id, "users": users}


@mcp.tool()
async def get_roster_user_mapping(league_id: str) -> dict:
    """Get a simple roster_id -> manager (username/display_name) mapping for a league."""
    rosters, users = await _get(f"/league/{league_id}/rosters"), await _get(f"/league/{league_id}/users")
    if not rosters:
        return {"error": f"No rosters found for league '{league_id}'"}
    user_map = {u["user_id"]: u for u in (users or [])}
    mapping = [
        {
            "roster_id": r.get("roster_id"),
            "owner_id": r.get("owner_id"),
            "username": user_map.get(r.get("owner_id"), {}).get("username"),
            "display_name": user_map.get(r.get("owner_id"), {}).get("display_name"),
        }
        for r in rosters
    ]
    mapping.sort(key=lambda x: x["roster_id"] or 0)
    return {"league_id": league_id, "roster_user_mapping": mapping}


@mcp.tool()
async def get_league_draft(league_id: str) -> dict:
    """Get complete draft results (picks, rounds, keeper flags) for a league."""
    league = await _get(f"/league/{league_id}")
    if not league or not league.get("draft_id"):
        return {"error": f"No draft found for league '{league_id}'"}
    draft_id = league["draft_id"]
    picks = await _get(f"/draft/{draft_id}/picks")
    users = await _get(f"/league/{league_id}/users")
    player_map = await _players_map()
    user_map = {u["user_id"]: u for u in (users or [])}

    formatted = []
    for p in picks or []:
        info = player_map.get(p.get("player_id"), {"full_name": f"Player {p.get('player_id')}"})
        owner = user_map.get(p.get("picked_by"), {})
        formatted.append(
            {
                "pick_no": p.get("pick_no"),
                "round": p.get("round"),
                "player": info,
                "drafted_by": owner.get("display_name") or owner.get("username"),
                "is_keeper": p.get("is_keeper", False),
            }
        )
    formatted.sort(key=lambda x: x["pick_no"] or 0)
    return {"league_id": league_id, "draft_id": draft_id, "picks": formatted}


@mcp.tool()
async def search_players(query: str, position: Optional[str] = None) -> dict:
    """Search Sleeper's NFL player catalog by (partial) name, optionally filtered by position."""
    player_map = await _players_map()
    q = query.lower()
    results = [
        {"player_id": pid, **info}
        for pid, info in player_map.items()
        if q in (info["full_name"] or "").lower() and (position is None or info.get("position") == position)
    ]
    return {"query": query, "position": position, "results": results[:50], "total_matches": len(results)}


@mcp.tool()
async def get_trending_players(sport: str = "nfl", add_drop: str = "add") -> dict:
    """Get currently trending players (most added or most dropped)."""
    data = await _get(f"/players/{sport}/trending/{add_drop}")
    player_map = await _players_map()
    return {
        "sport": sport,
        "type": add_drop,
        "players": [
            {"player_id": d["player_id"], "count": d["count"], **player_map.get(d["player_id"], {})}
            for d in (data or [])
        ],
    }


@mcp.tool()
async def get_matchups(league_id: str, week: int) -> dict:
    """Get matchups (rosters, points, starters) for a given week in a league."""
    data = await _get(f"/league/{league_id}/matchups/{week}")
    if data is None:
        return {"error": f"No matchup data for league '{league_id}' week {week}"}
    return {"league_id": league_id, "week": week, "matchups": data}


@mcp.tool()
async def get_player_stats(player_id: str, season: str, week: Optional[int] = None) -> dict:
    """Get a player's stats for a season, optionally narrowed to one week."""
    if week is not None:
        data = await _get(f"/stats/nfl/regular/{season}/{week}")
    else:
        data = await _get(f"/stats/nfl/regular/{season}")
    if not data:
        return {"error": "No stats found for that season/week"}
    stats = data.get(player_id)
    if stats is None:
        return {"error": f"No stats found for player '{player_id}'"}
    return {"player_id": player_id, "season": season, "week": week, "stats": stats}


# ---------------------------------------------------------------------------
# optional shared-secret auth middleware
# ---------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if AUTH_TOKEN:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {AUTH_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app():
    app = mcp.streamable_http_app()
    if AUTH_TOKEN:
        app.add_middleware(BearerAuthMiddleware)
        logger.info("Bearer auth enabled for this server.")
    else:
        logger.warning("SLEEPER_MCP_TOKEN not set -- server is running with NO auth.")
    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

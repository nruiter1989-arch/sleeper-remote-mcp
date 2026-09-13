# sleeper-remote-mcp

A small remote MCP server that wraps [Sleeper's public fantasy football API](https://docs.sleeper.app/)
over the Streamable HTTP transport, so it can be added to Claude (claude.ai /
Cowork) as a **custom connector** — no local process, no Claude Desktop
config file, just a URL.

## Why this exists

Sleeper's own roster data already separates `reserve` (IR) players from
bench players, but several popular wrappers around it collapse everything
non-starting into one flat "bench" list, making it impossible to reliably
tell who's actually stashed on IR right now. This server passes `reserve`
(IR) and `taxi` through untouched and enriched with player names, so "who's
on IR" is a direct read instead of a guess.

Loosely inspired by the tool surface of
[sourknives/sleeper-mcp-server](https://github.com/sourknives/sleeper-mcp-server)
(MIT licensed), rewritten from scratch here to run over HTTP instead of
stdio/Claude Desktop config.

## Tools

- `get_nfl_state` — current season/week
- `get_user_leagues(username, season)`
- `get_league_info(league_id)`
- `get_league_rosters(league_id)` — starters / bench / **reserve (IR)** / taxi, each enriched with player name, position, team, status
- `get_league_users(league_id)`
- `get_roster_user_mapping(league_id)`
- `get_league_draft(league_id)`
- `search_players(query, position?)`
- `get_trending_players(sport?, add_drop?)`
- `get_matchups(league_id, week)`
- `get_player_stats(player_id, season, week?)`

## Deploying

This is a plain Python/Starlette app — deployable anywhere that runs a
`Procfile`-style web process (Railway, Render, Fly.io, etc).

```bash
pip install -r requirements.txt
cp .env.example .env   # set SLEEPER_MCP_TOKEN to a long random string
python server.py       # listens on $PORT (default 8000), path /mcp
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SLEEPER_MCP_TOKEN` | recommended | Shared-secret bearer token. If set, every request must send `Authorization: Bearer <token>`. If unset, the server has **no auth** — fine for local testing, not recommended once it has a public URL. |
| `PORT` | no | Port to listen on. Most hosting platforms set this automatically. |

## Adding it to Claude as a custom connector

1. In claude.ai: **Customize → Connectors → + → Add custom connector** (Team/Enterprise: an org owner does this under Organization settings → Connectors).
2. URL: `https://<your-deployed-host>/mcp`
3. If you set `SLEEPER_MCP_TOKEN`, add it under Advanced settings as the bearer token / Authorization header.
4. Enable it for the chat/task you want to use it in. A brand-new session picks up newly connected tools automatically.

## License

MIT

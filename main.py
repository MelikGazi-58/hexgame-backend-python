from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import os
import random
from typing import Dict, List, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Renk sırası
colors_order = ["blue", "red", "green", "yellow"]


# ------------------------------
# PLAYER
# ------------------------------
class Player:
    def __init__(self, websocket: WebSocket, color: str):
        self.websocket = websocket
        self.color = color
        self.name: Optional[str] = None
        self.is_bot: bool = False

    @property
    def label(self):
        if self.name:
            return self.name
        if self.is_bot:
            return "ai"
        return self.color


# ------------------------------
# GAME STATE (ODA BAZLI)
# ------------------------------
class GameState:
    def __init__(self):
        self.started = False
        self.max_players = 4
        self.map_radius = 3
        self.difficulty = 2

        self.players_by_ws: Dict[WebSocket, Player] = {}
        self.players_by_color: Dict[str, Player] = {}

        self.cells: Dict[int, dict] = {}
        self.last_moves: List[dict] = []
        self.current_player_color: Optional[str] = None

        self.lock = asyncio.Lock()

    def reset_game(self):
        self.started = False
        self.cells = {}
        self.last_moves = []
        self.current_player_color = None

    def players_info_payload(self):
        info = {}
        for col, p in self.players_by_color.items():
            info[col] = {"name": p.name, "is_bot": p.is_bot}
        return info

    def stats(self):
        stats = {c: {"cells": 0, "troops": 0} for c in colors_order}
        for cell in self.cells.values():
            owner = cell.get("owner")
            if owner in stats:
                stats[owner]["cells"] += 1
                stats[owner]["troops"] += cell.get("troops", 0)
        return stats

    def alive_colors(self):
        s = self.stats()
        return [c for c in colors_order if s[c]["cells"] > 0]


# ------------------------------
# TÜM ODALAR
# ------------------------------
rooms: Dict[str, GameState] = {}


def get_room(room_id: str) -> GameState:
    if room_id not in rooms:
        rooms[room_id] = GameState()
    return rooms[room_id]


# ------------------------------
# HELPER FUNCTIONS
# ------------------------------
async def send_json_safe(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except:
        pass


async def broadcast(room: GameState, payload: dict):
    text = json.dumps(payload)
    dead = []
    for ws in list(room.players_by_ws.keys()):
        try:
            await ws.send_text(text)
        except:
            dead.append(ws)

    for ws in dead:
        await unregister(room, ws)


async def unregister(room: GameState, ws: WebSocket):
    async with room.lock:
        player = room.players_by_ws.pop(ws, None)
        if not player:
            return

        room.players_by_color.pop(player.color, None)

        if not room.players_by_ws:
            room.reset_game()
            return

        if room.started:
            for cell in room.cells.values():
                if cell.get("owner") == player.color:
                    cell["owner"] = None
                    cell["troops"] = 0

            alive = room.alive_colors()
            if len(alive) == 1:
                winner = alive[0]
                for p in room.players_by_ws.values():
                    result = "win" if p.color == winner else "lose"
                    await send_json_safe(p.websocket, {"type": "game_over", "result": result})
                room.reset_game()
            else:
                if room.current_player_color == player.color:
                    room.current_player_color = next_player_color(room)

        await send_lobby(room)


async def send_lobby(room: GameState):
    payload = {
        "type": "lobby",
        "started": room.started,
        "players_info": room.players_info_payload(),
        "max_players": room.max_players,
        "map_radius": room.map_radius,
        "difficulty": room.difficulty
    }
    await broadcast(room, payload)


def next_player_color(room: GameState) -> Optional[str]:
    alive = room.alive_colors()
    if not alive:
        return None
    if room.current_player_color not in alive:
        return alive[0]
    idx = alive.index(room.current_player_color)
    return alive[(idx + 1) % len(alive)]


def build_map(radius: int):
    cells = {}
    cid = 0
    R = max(1, min(radius, 6))
    for q in range(-R, R + 1):
        r1 = max(-R, -q - R)
        r2 = min(R, -q + R)
        for r in range(r1, r2 + 1):
            cells[cid] = {"id": cid, "q": q, "r": r, "owner": None, "troops": 0}
            cid += 1
    return cells


def are_neighbors(id1, id2, cells):
    s = cells.get(id1)
    d = cells.get(id2)
    if not s or not d:
        return False
    dq = d["q"] - s["q"]
    dr = d["r"] - s["r"]
    ds = -dq - dr
    return (abs(dq), abs(dr), abs(ds)) in [(1, 0, 1), (1, 1, 0), (0, 1, 1)]


def apply_transfer(room, color, src, dst, amount):
    cells = room.cells
    if src not in cells or dst not in cells:
        return None

    s = cells[src]
    d = cells[dst]

    if s["owner"] != color:
        return None
    if amount <= 0 or s["troops"] < amount:
        return None
    if not are_neighbors(src, dst, cells):
        return None

    s["troops"] -= amount

    if d["owner"] is None:
        d["owner"] = color
        d["troops"] = amount
        return "occupy"

    if d["owner"] == color:
        d["troops"] += amount
        return "transfer"

    # battle
    if amount > d["troops"]:
        d["owner"] = color
        d["troops"] = amount - d["troops"]
    else:
        d["troops"] -= amount
    return "battle"


async def check_game_over(room: GameState):
    alive = room.alive_colors()
    if len(alive) == 1:
        winner = alive[0]
        for p in room.players_by_ws.values():
            result = "win" if p.color == winner else "lose"
            await send_json_safe(p.websocket, {"type": "game_over", "result": result})
        room.reset_game()
        await send_lobby(room)
        return True
    return False


async def broadcast_state(room: GameState):
    payload = {
        "type": "state",
        "cells": room.cells,
        "moves": room.last_moves,
        "current_player": room.current_player_color,
        "players_info": room.players_info_payload(),
        "started": room.started,
        "max_players": room.max_players,
        "map_radius": room.map_radius,
        "difficulty": room.difficulty,
    }
    await broadcast(room, payload)


# ------------------------------
# WEBSOCKET ENDPOINT
# ------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # ROOM ID OKU
    room_id = ws.query_params.get("room", "default")
    room = get_room(room_id)

    # PLAYER KAYDEDİLİYOR
    async with room.lock:
        free = None
        for c in colors_order:
            if c not in room.players_by_color:
                free = c
                break

        if free is None:
            await send_json_safe(ws, {"type": "error", "message": "Oda dolu"})
            await ws.close()
            return

        player = Player(ws, free)
        room.players_by_ws[ws] = player
        room.players_by_color[free] = player

        await send_json_safe(ws, {"type": "you_are", "color": free})
        await send_lobby(room)

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            async with room.lock:
                player = room.players_by_ws.get(ws)
                if not player:
                    continue

                m = msg.get("type")

                # CONFIG
                if m == "config":
                    room.max_players = int(msg.get("max_players", 2))
                    await send_lobby(room)
                    continue

                if m == "config_map":
                    room.map_radius = int(msg.get("map_radius", 3))
                    await send_lobby(room)
                    continue

                if m == "config_difficulty":
                    room.difficulty = int(msg.get("difficulty", 2))
                    await send_lobby(room)
                    continue

                # NAME
                if m == "set_name":
                    name = msg.get("name", "").strip()
                    if name:
                        player.name = name[:20]
                    await send_lobby(room)
                    continue

                # EMOJI
                if m == "emoji":
                    await broadcast(room, {
                        "type": "emoji",
                        "emoji": msg.get("emoji", "🙂"),
                        "from": player.label
                    })
                    continue

                # START
                if m == "start":
                    if not room.started:
                        if len(room.players_by_ws) < 2:
                            await send_json_safe(ws, {"type": "error", "message": "En az 2 oyuncu gerekir"})
                        else:
                            room.cells = build_map(room.map_radius)

                            order = list(room.players_by_ws.values())
                            random.shuffle(order)

                            ids = list(room.cells.keys())
                            random.shuffle(ids)

                            used = set()
                            for p in order:
                                cid = None
                                for idd in ids:
                                    if idd not in used:
                                        cid = idd
                                        used.add(idd)
                                        break
                                room.cells[cid]["owner"] = p.color
                                room.cells[cid]["troops"] = 10

                            room.started = True
                            room.last_moves = []

                            for c in colors_order:
                                if c in room.players_by_color:
                                    room.current_player_color = c
                                    break

                            await broadcast(room, {
                                "type": "start_game",
                                "cells": room.cells,
                                "moves": room.last_moves,
                                "current_player": room.current_player_color,
                                "players_info": room.players_info_payload(),
                            })
                    continue

                # TRANSFER
                if m == "transfer":
                    if not room.started:
                        continue
                    if room.current_player_color != player.color:
                        continue

                    src = int(msg["source"])
                    dst = int(msg["target"])
                    amt = int(msg["amount"])

                    kind = apply_transfer(room, player.color, src, dst, amt)
                    if not kind:
                        continue

                    room.last_moves.append({"src": src, "dst": dst, "color": player.color})
                    room.last_moves = room.last_moves[-8:]

                    await broadcast(room, {"type": "transfer_event", "kind": kind})

                    for c in room.cells.values():
                        if c["owner"] == player.color:
                            c["troops"] = min(100, c["troops"] + 1)

                    owned = [cid for cid, c in room.cells.items() if c["owner"] == player.color]
                    if owned:
                        cid = random.choice(owned)
                        extra = random.randint(1, 3)
                        room.cells[cid]["troops"] = min(100, room.cells[cid]["troops"] + extra)
                        await broadcast(room, {
                            "type": "bonus",
                            "color": player.color,
                            "cell": cid,
                            "amount": extra
                        })

                    finished = await check_game_over(room)
                    if finished:
                        continue

                    room.current_player_color = next_player_color(room)
                    await broadcast_state(room)
                    continue

    except WebSocketDisconnect:
        await unregister(room, ws)


# ------------------------------
# RUN
# ------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

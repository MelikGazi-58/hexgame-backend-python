
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import os
import random
from typing import Dict, List, Optional

app = FastAPI()

# CORS - istersen origin kısıtlayabilirsin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Oyuncu renkleri (sabit sıra)
colors_order = ["blue", "red", "green", "yellow"]


class Player:
    def __init__(self, websocket: WebSocket, color: str):
        self.websocket = websocket
        self.color = color
        self.name: Optional[str] = None
        self.is_bot: bool = False

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.is_bot:
            return "ai"
        return self.color


class GameState:
    """
    Tek bir oda (room) içindeki oyunun tüm state'i.
    """
    def __init__(self, room_id: str):
        self.room_id = room_id

        # Lobby config
        self.started: bool = False
        self.max_players: int = 4
        self.map_radius: int = 3
        self.difficulty: int = 2

        # Oyuncular & bağlantılar
        self.players_by_ws: Dict[WebSocket, Player] = {}
        self.players_by_color: Dict[str, Player] = {}

        # Oyun durumu
        self.cells: Dict[int, dict] = {}        # id -> {q,r,owner,troops}
        self.last_moves: List[dict] = []        # [{src,dst,color}]
        self.current_player_color: Optional[str] = None

        # Senkronizasyon
        self.lock = asyncio.Lock()

    def reset_game(self):
        self.started = False
        self.cells = {}
        self.last_moves = []
        self.current_player_color = None

    def players_info_payload(self) -> Dict[str, dict]:
        info = {}
        for color, player in self.players_by_color.items():
            info[color] = {
                "name": player.name,
                "is_bot": player.is_bot,
            }
        return info

    def stats(self) -> Dict[str, dict]:
        stats = {c: {"cells": 0, "troops": 0} for c in colors_order}
        for cell in self.cells.values():
            owner = cell.get("owner")
            if owner in stats:
                stats[owner]["cells"] += 1
                stats[owner]["troops"] += cell.get("troops", 0)
        return stats

    def alive_colors(self) -> List[str]:
        s = self.stats()
        return [c for c in colors_order if s[c]["cells"] > 0]


# Birden fazla room'u yöneten basit yapı
class RoomManager:
    def __init__(self):
        # room_id -> GameState
        self.rooms: Dict[str, GameState] = {}
        # Room listesinde değişiklik olduğunda kilit
        self.lock = asyncio.Lock()

    async def get_room(self, room_id: str) -> GameState:
        """
        İstenen room_id için GameState döndür. Yoksa yarat.
        """
        async with self.lock:
            if room_id not in self.rooms:
                self.rooms[room_id] = GameState(room_id)
            return self.rooms[room_id]

    async def cleanup_room_if_empty(self, room: GameState):
        """
        Odada hiç player kalmadıysa room'u tamamen sil.
        """
        async with self.lock:
            if not room.players_by_ws:
                self.rooms.pop(room.room_id, None)


room_manager = RoomManager()


async def send_json_safe(ws: WebSocket, payload: dict):
    """Tek bir client’a güvenli JSON yolla."""
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        # Bağlantı gitmiş olabilir, sessizce geç
        pass


async def broadcast(game: GameState, payload: dict):
    """Belirli bir oda içindeki tüm oyunculara mesaj yolla."""
    text = json.dumps(payload)
    dead = []
    for ws in list(game.players_by_ws.keys()):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        await unregister(game, ws)


async def send_lobby(game: GameState):
    """Belirli oda için lobby state broadcast."""
    payload = {
        "type": "lobby",
        "started": game.started,
        "players_info": game.players_info_payload(),
        "max_players": game.max_players,
        "map_radius": game.map_radius,
        "difficulty": game.difficulty,
    }
    await broadcast(game, payload)


async def unregister(game: GameState, ws: WebSocket):
    """Oyuncu disconnect olduğunda cleanup (oda bazlı)."""
    async with game.lock:
        player = game.players_by_ws.pop(ws, None)
        if not player:
            return

        game.players_by_color.pop(player.color, None)

        # Hiç oyuncu kalmadıysa: komple reset
        if not game.players_by_ws:
            game.reset_game()
        else:
            # Oyun başladıysa: bu oyuncunun hücrelerini nötr yap
            if game.started:
                for cell in game.cells.values():
                    if cell.get("owner") == player.color:
                        cell["owner"] = None
                        cell["troops"] = 0

                alive = game.alive_colors()
                # Tek renk kaldıysa: o kazanır
                if len(alive) == 1:
                    winner_color = alive[0]
                    for p in game.players_by_ws.values():
                        result = "win" if p.color == winner_color else "lose"
                        await send_json_safe(
                            p.websocket,
                            {"type": "game_over", "result": result},
                        )
                    game.reset_game()
                else:
                    # Sıra ondaysa, sırayı bir sonrakine geçir
                    if game.current_player_color == player.color:
                        game.current_player_color = next_player_color(game)

        await send_lobby(game)
        # Oda boşalmışsa manager'dan sil
        await room_manager.cleanup_room_if_empty(game)


def next_player_color(game: GameState) -> Optional[str]:
    """Sıra bir sonraki yaşayan renge geçsin."""
    alive = game.alive_colors()
    if not alive:
        return None
    if game.current_player_color not in alive:
        return alive[0]
    idx = alive.index(game.current_player_color)
    return alive[(idx + 1) % len(alive)]


def build_map(radius: int):
    """Axial hex disk map (q,r)."""
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


def are_neighbors(src_id: int, dst_id: int, cells: Dict[int, dict]) -> bool:
    """İki hücre komşu mu (axial hex)?"""
    s = cells.get(src_id)
    d = cells.get(dst_id)
    if not s or not d:
        return False

    dq = d["q"] - s["q"]
    dr = d["r"] - s["r"]
    ds = -dq - dr

    # 6 komşu yönü için abs fark kombinasyonları
    if (abs(dq), abs(dr), abs(ds)) in [
        (1, 0, 1),
        (1, 1, 0),
        (0, 1, 1),
    ]:
        return True
    return False


def apply_transfer(game: GameState, player_color: str, source: int, target: int, amount: int) -> Optional[str]:
    """
    Transfer logic:
    - Komşu değilse / sahibi değilsen: None
    - Boş hücre ise: occupy
    - Aynı renk ise: transfer
    - Farklı renk ise: battle
    """
    cells = game.cells
    if source not in cells or target not in cells:
        return None
    src = cells[source]
    dst = cells[target]

    if src.get("owner") != player_color:
        return None
    if amount <= 0 or src.get("troops", 0) < amount:
        return None

    if not are_neighbors(source, target, cells):
        return None

    src["troops"] -= amount

    # Boş hücre
    if dst.get("owner") is None:
        dst["owner"] = player_color
        dst["troops"] = amount
        return "occupy" if amount > 0 else "transfer"

    # Aynı oyuncu -> birleştir
    if dst["owner"] == player_color:
        dst["troops"] = dst.get("troops", 0) + amount
        return "transfer"

    # Savaş
    defender_troops = dst.get("troops", 0)
    if amount > defender_troops:
        dst["owner"] = player_color
        dst["troops"] = amount - defender_troops
    else:
        dst["troops"] = defender_troops - amount
    return "battle"


async def check_game_over(game: GameState):
    """Tek renk kaldı mı diye bak; bitti ise game_over gönder."""
    alive = game.alive_colors()
    if len(alive) == 1:
        winner = alive[0]
        for p in game.players_by_ws.values():
            result = "win" if p.color == winner else "lose"
            await send_json_safe(p.websocket, {"type": "game_over", "result": result})
        game.reset_game()
        await send_lobby(game)
        return True
    return False


async def broadcast_state(game: GameState):
    """Frontend’in beklediği state payload’u."""
    payload = {
        "type": "state" if game.started else "lobby",
        "cells": game.cells if game.started else None,
        "moves": game.last_moves if game.started else [],
        "current_player": game.current_player_color,
        "players_info": game.players_info_payload(),
        "started": game.started,
        "max_players": game.max_players,
        "map_radius": game.map_radius,
        "difficulty": game.difficulty,
    }
    await broadcast(game, payload)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # room parametresini al (yoksa 'default')
    room_id = websocket.query_params.get("room") or "default"
    game = await room_manager.get_room(room_id)

    await websocket.accept()

    # Yeni gelen oyuncuya renk ata
    async with game.lock:
        free_color = None
        for c in colors_order:
            if c not in game.players_by_color:
                free_color = c
                break

        if free_color is None:
            await send_json_safe(websocket, {"type": "error", "message": "Lobby dolu"})
            await websocket.close()
            return

        player = Player(websocket, free_color)
        game.players_by_ws[websocket] = player
        game.players_by_color[free_color] = player

        # YOU_ARE
        await send_json_safe(websocket, {"type": "you_are", "color": free_color})
        await send_lobby(game)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send_json_safe(websocket, {"type": "error", "message": "Geçersiz JSON"})
                continue

            if not isinstance(msg, dict):
                continue

            async with game.lock:
                player = game.players_by_ws.get(websocket)
                if not player:
                    continue

                mtype = msg.get("type")

                # ---- CONFIG ----
                if mtype == "config":
                    v = int(msg.get("max_players", 2))
                    game.max_players = max(2, min(4, v))
                    await send_lobby(game)
                    continue

                if mtype == "config_map":
                    v = int(msg.get("map_radius", 3))
                    game.map_radius = max(2, min(6, v))
                    await send_lobby(game)
                    continue

                if mtype == "config_difficulty":
                    v = int(msg.get("difficulty", 2))
                    game.difficulty = max(1, min(3, v))
                    await send_lobby(game)
                    continue

                # ---- NAME ----
                if mtype == "set_name":
                    name = str(msg.get("name", "")).strip()
                    if name:
                        player.name = name[:20]
                    await send_lobby(game)
                    continue

                # ---- EMOJI ----
                if mtype == "emoji":
                    emo = msg.get("emoji", "🙂")
                    payload = {
                        "type": "emoji",
                        "emoji": emo,
                        "from": player.label,
                    }
                    await broadcast(game, payload)
                    continue

                # ---- START GAME ----
                if mtype == "start":
                    if not game.started:
                        if len(game.players_by_ws) < 2:
                            await send_json_safe(
                                websocket,
                                {"type": "error", "message": "En az 2 oyuncu gerekir"},
                            )
                        else:
                            # Haritayı kur
                            game.cells = build_map(game.map_radius)

                            # Her oyuncuya rastgele bir başlangıç hücresi
                            cell_ids = list(game.cells.keys())
                            random.shuffle(cell_ids)
                            used = set()
                            for p in game.players_by_ws.values():
                                cid = None
                                for idx in cell_ids:
                                    if idx not in used:
                                        cid = idx
                                        used.add(idx)
                                        break
                                if cid is None:
                                    continue
                                game.cells[cid]["owner"] = p.color
                                game.cells[cid]["troops"] = 10

                            game.started = True
                            game.last_moves = []

                            # İlk sıra ilk renge
                            for c in colors_order:
                                if c in game.players_by_color:
                                    game.current_player_color = c
                                    break

                            payload = {
                                "type": "start_game",
                                "cells": game.cells,
                                "moves": game.last_moves,
                                "current_player": game.current_player_color,
                                "players_info": game.players_info_payload(),
                            }
                            await broadcast(game, payload)
                    continue

                # ---- TRANSFER ----
                if mtype == "transfer":
                    if not game.started:
                        continue
                    if game.current_player_color != player.color:
                        # Sıra sende değil
                        continue

                    try:
                        source = int(msg.get("source"))
                        target = int(msg.get("target"))
                        amount = int(msg.get("amount", 0))
                    except (TypeError, ValueError):
                        continue

                    kind = apply_transfer(game, player.color, source, target, amount)
                    if not kind:
                        continue

                    # Son hamleler
                    game.last_moves.append(
                        {"src": source, "dst": target, "color": player.color}
                    )
                    if len(game.last_moves) > 8:
                        game.last_moves = game.last_moves[-8:]

                    # transfer_event
                    await broadcast(game, {"type": "transfer_event", "kind": kind})

                    # Basit bonus: kendi hücrelerine +1 (max 100)
                    for cell in game.cells.values():
                        if cell.get("owner") == player.color:
                            cell["troops"] = min(100, cell.get("troops", 0) + 1)

                    # Rastgele ekstra bonus
                    owned_cells = [
                        c["id"] for c in game.cells.values()
                        if c.get("owner") == player.color
                    ]
                    if owned_cells:
                        cid = random.choice(owned_cells)
                        bonus_amt = random.randint(1, 3)
                        cell = game.cells[cid]
                        cell["troops"] = min(100, cell.get("troops", 0) + bonus_amt)
                        await broadcast(
                            game,
                            {
                                "type": "bonus",
                                "color": player.color,
                                "cell": cid,
                                "amount": bonus_amt,
                            }
                        )

                    # Game over kontrol
                    finished = await check_game_over(game)
                    if finished:
                        continue

                    # Sırayı sonraki oyuncuya ver
                    game.current_player_color = next_player_color(game)
                    await broadcast_state(game)
                    continue

                # Bilinmeyen type: ignore
    except WebSocketDisconnect:
        await unregister(game, websocket)
    except Exception:
        await unregister(game, websocket)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)

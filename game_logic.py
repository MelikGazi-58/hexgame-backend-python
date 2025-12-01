import random


class GameState:
    def __init__(self, max_players=2, map_radius=3, difficulty=2):
        self.max_players = max_players
        self.map_radius = map_radius
        self.difficulty = difficulty

        self.players = {}  # color → {ws, name, is_bot}
        self.colors = ["blue", "red", "green", "yellow"]
        self.started = False

        self.cells = {}
        self.current_player = None
        self.moves = []

    def set_map(self, cells):
        self.cells = cells

    def add_player(self, ws):
        for col in self.colors:
            if col not in self.players:
                self.players[col] = {
                    "ws": ws,
                    "name": None,
                    "is_bot": False
                }
                return col
        return None

    def to_lobby(self):
        return {
            "type": "lobby",
            "started": self.started,
            "max_players": self.max_players,
            "map_radius": self.map_radius,
            "difficulty": self.difficulty,
            "players_info": {
                col: {"name": p["name"], "is_bot": p["is_bot"]}
                for col, p in self.players.items()
            }
        }

    def to_state(self):
        return {
            "type": "state",
            "cells": self.cells,
            "moves": self.moves,
            "current_player": self.current_player,
            "players_info": {
                col: {"name": p["name"], "is_bot": p["is_bot"]}
                for col, p in self.players.items()
            }
        }

    def start(self):
        self.started = True

        # Her renge 1 hücre ver
        ids = list(self.cells.keys())
        random.shuffle(ids)

        i = 0
        for col in self.players.keys():
            cid = ids[i]
            self.cells[cid]["owner"] = col
            self.cells[cid]["troops"] = 20
            i += 1

        self.current_player = list(self.players.keys())[0]

    def transfer(self, src, dst, amount):
        if src not in self.cells or dst not in self.cells:
            return None

        s = self.cells[src]
        d = self.cells[dst]

        if s["owner"] != self.current_player:
            return None

        if amount > s["troops"]:
            return None

        # Ücret
        s["troops"] -= amount

        # Boşsa işgal
        if d["owner"] is None:
            d["owner"] = self.current_player
            d["troops"] = amount
            return "occupy"

        # Kendi rengi ise transfer
        if d["owner"] == self.current_player:
            d["troops"] += amount
            return "transfer"

        # Savaş
        result = amount - d["troops"]
        if result > 0:
            d["owner"] = self.current_player
            d["troops"] = result
            return "battle"
        else:
            d["troops"] = -result
            return "battle"

    def next_turn(self):
        cols = list(self.players.keys())
        idx = cols.index(self.current_player)
        self.current_player = cols[(idx + 1) % len(cols)]

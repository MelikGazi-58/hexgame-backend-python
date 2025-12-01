from pydantic import BaseModel
from typing import Optional

class WsMessage(BaseModel):
    type: str
    name: Optional[str] = None
    max_players: Optional[int] = None
    map_radius: Optional[int] = None
    difficulty: Optional[int] = None
    source: Optional[int] = None
    target: Optional[int] = None
    amount: Optional[int] = None
    emoji: Optional[str] = None

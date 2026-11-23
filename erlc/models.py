"""Typed views of documented ER:LC API v2 response objects.

Only fields listed in the official documentation (https://apidocs.erlc.gg/) are
read. Unknown fields are ignored; missing fields become None - never invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


@dataclass
class PlayerLocation:
    x: Optional[float]
    z: Optional[float]
    postal: Optional[str]
    street: Optional[str]
    building: Optional[str]

    @classmethod
    def parse(cls, data: Any) -> Optional["PlayerLocation"]:
        if not isinstance(data, dict):
            return None
        return cls(
            x=_float(data.get("LocationX")),
            z=_float(data.get("LocationZ")),
            postal=_str(data.get("PostalCode")),
            street=_str(data.get("StreetName")),
            building=_str(data.get("BuildingNumber")),
        )


@dataclass
class Player:
    name: str
    roblox_id: Optional[int]
    team: Optional[str]
    callsign: Optional[str]
    permission: Optional[str]
    wanted_stars: Optional[int]
    location: Optional[PlayerLocation]

    @classmethod
    def parse(cls, data: dict) -> "Player":
        raw = str(data.get("Player") or "")
        # Documented format: "PlayerName:Id"
        name, _, rid = raw.rpartition(":")
        if not name:
            name, rid = raw, ""
        return cls(
            name=name,
            roblox_id=_int(rid),
            team=_str(data.get("Team")),
            callsign=_str(data.get("Callsign")),
            permission=_str(data.get("Permission")),
            wanted_stars=_int(data.get("WantedStars")),
            location=PlayerLocation.parse(data.get("Location")),
        )


@dataclass
class EmergencyCall:
    """One entry of the documented v2 `EmergencyCalls` array."""

    call_number: int
    started_at: int
    team: Optional[str]
    caller_id: Optional[int]
    responders: list[int]
    position: Optional[tuple[float, float]]
    description: Optional[str]
    position_descriptor: Optional[str]
    raw: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict) -> Optional["EmergencyCall"]:
        number = _int(data.get("CallNumber"))
        started = _int(data.get("StartedAt"))
        if number is None or started is None:
            return None  # cannot be de-duplicated safely -> rejected by validation
        pos = data.get("Position")
        position = None
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            px, pz = _float(pos[0]), _float(pos[1])
            if px is not None and pz is not None:
                position = (px, pz)
        players = data.get("Players")
        responders = [p for p in (_int(x) for x in players)] if isinstance(players, list) else []
        return cls(
            call_number=number,
            started_at=started,
            team=_str(data.get("Team")),
            caller_id=_int(data.get("Caller")),
            responders=[r for r in responders if r is not None],
            position=position,
            description=_str(data.get("Description")),
            position_descriptor=_str(data.get("PositionDescriptor")),
            raw=dict(data),
        )


@dataclass
class ServerSnapshot:
    name: Optional[str]
    owner_id: Optional[int]
    current_players: Optional[int]
    max_players: Optional[int]
    join_key: Optional[str]
    players: list[Player]
    emergency_calls: list[EmergencyCall]
    emergency_calls_present: bool  # whether the API actually returned the EmergencyCalls field
    invalid_calls: int = 0

    @classmethod
    def parse(cls, data: dict) -> "ServerSnapshot":
        players = [Player.parse(p) for p in data.get("Players") or [] if isinstance(p, dict)]
        calls: list[EmergencyCall] = []
        invalid = 0
        raw_calls = data.get("EmergencyCalls")
        for c in raw_calls or []:
            parsed = EmergencyCall.parse(c) if isinstance(c, dict) else None
            if parsed is None:
                invalid += 1
            else:
                calls.append(parsed)
        return cls(
            name=_str(data.get("Name")),
            owner_id=_int(data.get("OwnerId")),
            current_players=_int(data.get("CurrentPlayers")),
            max_players=_int(data.get("MaxPlayers")),
            join_key=_str(data.get("JoinKey")),
            players=players,
            emergency_calls=calls,
            emergency_calls_present="EmergencyCalls" in data,
            invalid_calls=invalid,
        )

    def player_by_id(self, roblox_id: Optional[int]) -> Optional[Player]:
        if roblox_id is None:
            return None
        for p in self.players:
            if p.roblox_id == roblox_id:
                return p
        return None

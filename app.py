from __future__ import annotations

import asyncio
import io
import random
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any

import qrcode
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from game_engine import CATALOG, GAME_CLASSES, P, create_game

BASE = Path(__file__).parent
app = FastAPI(title="みんなのトランプ")


class CreateRoomBody(BaseModel):
    name: str = "PLAYER"
    mode: str = "online"  # online / solo
    game_id: str = "daifugo"
    difficulty: str = "normal"


class JoinBody(BaseModel):
    name: str = "PLAYER"


@dataclass
class Room:
    code: str
    host_token: str
    players: list[P] = field(default_factory=list)
    token_to_pid: Dict[str, str] = field(default_factory=dict)
    sockets: Dict[str, WebSocket] = field(default_factory=dict)
    game_id: str = "daifugo"
    game: Any = None
    rules: Dict[str, Any] = field(default_factory=dict)
    session_score: Dict[str, int] = field(default_factory=dict)
    bot_seq: int = 0
    last_result_key: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    bot_task: Optional[asyncio.Task] = None

    def get_player(self, pid: str) -> Optional[P]:
        return next((p for p in self.players if p.id == pid), None)

    def human_count(self) -> int:
        return sum(not p.is_bot for p in self.players)

    def max_players(self) -> int:
        return GAME_CLASSES[self.game_id].max_players

    def add_bot(self, level: str = "normal") -> P:
        if len(self.players) >= self.max_players():
            raise ValueError("これ以上追加できません")
        self.bot_seq += 1
        p = P(id=f"bot-{self.bot_seq}-{secrets.token_hex(2)}", name=f"CPU {self.bot_seq}", is_bot=True, level=level)
        self.players.append(p)
        self.session_score.setdefault(p.id, 0)
        return p

    def remove_bot(self, pid: str):
        self.players = [p for p in self.players if not (p.id == pid and p.is_bot)]
        self.session_score.pop(pid, None)

    def start_game(self):
        cls = GAME_CLASSES[self.game_id]
        if not (cls.min_players <= len(self.players) <= cls.max_players):
            raise ValueError(f"{cls.name}は{cls.min_players}〜{cls.max_players}人です")
        self.game = create_game(self.game_id, self.players, seed=random.randint(1, 2_000_000_000), rules=self.rules)
        self.last_result_key = ""

    def room_view(self, token: str) -> Dict[str, Any]:
        pid = self.token_to_pid.get(token)
        host = token == self.host_token
        pmap = {p.id: p.name for p in self.players}
        base = {
            "type": "state",
            "room": self.code,
            "host": host,
            "you": pid,
            "game_id": self.game_id,
            "catalog": CATALOG,
            "players": [
                {"id": p.id, "name": p.name, "is_bot": p.is_bot, "level": p.level,
                 "session_score": self.session_score.get(p.id, 0)}
                for p in self.players
            ],
            "started": self.game is not None and not self.game.finished,
            "has_game": self.game is not None,
        }
        if self.game:
            gv = self.game.view(pid) if pid else {"game_name": self.game.name, "finished": self.game.finished, "table": self.game.table_view("")}
            gv["player_names"] = pmap
            base["game"] = gv
        return base

    def maybe_record_result(self):
        if not self.game or not self.game.finished:
            return
        key = f"{id(self.game)}:{','.join(self.game.winners)}"
        if key == self.last_result_key:
            return
        self.last_result_key = key
        if self.game.winners:
            for i, pid in enumerate(self.game.winners):
                self.session_score[pid] = self.session_score.get(pid, 0) + max(1, 3 - i)


rooms: Dict[str, Room] = {}


def clean_name(s: str) -> str:
    s = (s or "PLAYER").strip()
    return s[:16] or "PLAYER"


def new_code() -> str:
    for _ in range(1000):
        code = f"{random.randint(0,9999):04d}"
        if code not in rooms:
            return code
    raise RuntimeError("room code exhausted")


async def send_state(room: Room):
    room.maybe_record_result()
    dead = []
    for token, ws in list(room.sockets.items()):
        try:
            await ws.send_json(room.room_view(token))
        except Exception:
            dead.append(token)
    for t in dead:
        room.sockets.pop(t, None)


async def run_bots(room: Room, max_steps: int = 80):
    async with room.lock:
        steps = 0
        while room.game and not room.game.finished and steps < max_steps:
            steps += 1
            if room.game.real_time:
                bots = [p for p in room.players if p.is_bot]
                if not bots:
                    break
                moved = False
                for p in bots:
                    action = room.game.bot_action(p.id, p.level)
                    if action:
                        room.game.act(p.id, action)
                        moved = True
                        await send_state(room)
                        await asyncio.sleep({"easy": 1.2, "normal": 0.8, "hard": 0.5}.get(p.level, 0.8))
                        if room.game.finished:
                            break
                if room.human_count() > 0 or not moved:
                    break
                continue
            pid = room.game.current_pid
            p = room.get_player(pid) if pid else None
            if not p or not p.is_bot:
                break
            await asyncio.sleep({"easy": 0.95, "normal": 0.65, "hard": 0.45}.get(p.level, 0.65))
            action = room.game.bot_action(pid, p.level)
            if not action:
                break
            ok, _ = room.game.act(pid, action)
            await send_state(room)
            if not ok:
                break
        room.maybe_record_result()
        await send_state(room)


async def schedule_bots(room: Room):
    if room.bot_task and not room.bot_task.done():
        return
    room.bot_task = asyncio.create_task(run_bots(room))


@app.get("/api/catalog")
def catalog():
    return CATALOG


@app.post("/api/rooms")
async def create_room(body: CreateRoomBody):
    if body.game_id not in GAME_CLASSES:
        raise HTTPException(400, "ゲームが見つかりません")
    code = new_code()
    token = secrets.token_urlsafe(18)
    pid = f"p-{secrets.token_hex(5)}"
    p = P(pid, clean_name(body.name), False, "normal")
    room = Room(code=code, host_token=token, players=[p], token_to_pid={token: pid}, game_id=body.game_id)
    room.session_score[pid] = 0
    rooms[code] = room
    if body.mode == "solo":
        cls = GAME_CLASSES[body.game_id]
        target = max(cls.min_players, cls.recommended)
        while len(room.players) < target:
            room.add_bot(body.difficulty if body.difficulty in ("easy","normal","hard") else "normal")
        room.start_game()
        await schedule_bots(room)
    return {"code": code, "token": token, "pid": pid, "host": True}


@app.post("/api/rooms/{code}/join")
def join_room(code: str, body: JoinBody):
    room = rooms.get(code)
    if not room:
        raise HTTPException(404, "部屋が見つかりません")
    if room.game and not room.game.finished:
        raise HTTPException(409, "ゲーム中です")
    if len(room.players) >= room.max_players():
        raise HTTPException(409, "満員です")
    token = secrets.token_urlsafe(18)
    pid = f"p-{secrets.token_hex(5)}"
    p = P(pid, clean_name(body.name), False, "normal")
    room.players.append(p); room.token_to_pid[token] = pid; room.session_score[pid] = 0
    return {"code": code, "token": token, "pid": pid, "host": False}


@app.get("/room/{code}/qr")
def room_qr(code: str, request: Request):
    if code not in rooms:
        raise HTTPException(404, "部屋が見つかりません")
    origin = str(request.base_url).rstrip("/")
    url = f"{origin}/join/{code}"
    img = qrcode.make(url)
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.websocket("/ws/{code}")
async def websocket_room(ws: WebSocket, code: str, token: str):
    room = rooms.get(code)
    if not room or token not in room.token_to_pid:
        await ws.close(code=4404)
        return
    await ws.accept(); room.sockets[token] = ws
    await ws.send_json(room.room_view(token))
    await schedule_bots(room)
    try:
        while True:
            msg = await ws.receive_json()
            cmd = msg.get("cmd")
            pid = room.token_to_pid.get(token)
            is_host = token == room.host_token
            error = None
            if cmd == "set_game":
                gid = msg.get("game_id")
                if not is_host: error = "ホストのみ変更できます"
                elif gid not in GAME_CLASSES: error = "ゲームが見つかりません"
                elif room.game and not room.game.finished: error = "ゲーム中は変更できません"
                else:
                    room.game_id = gid; room.game = None; room.rules = msg.get("rules") or {}
                    mx = GAME_CLASSES[gid].max_players
                    while len(room.players) > mx and any(p.is_bot for p in room.players):
                        b = next(p for p in reversed(room.players) if p.is_bot); room.remove_bot(b.id)
                    if len(room.players) > mx: error = f"{GAME_CLASSES[gid].name}は最大{mx}人です"
            elif cmd == "add_bot":
                if not is_host: error = "ホストのみ追加できます"
                elif room.game and not room.game.finished: error = "ゲーム中です"
                else:
                    try: room.add_bot(msg.get("level","normal"))
                    except ValueError as e: error = str(e)
            elif cmd == "remove_bot":
                if not is_host: error = "ホストのみ削除できます"
                elif room.game and not room.game.finished: error = "ゲーム中です"
                else: room.remove_bot(msg.get("pid",""))
            elif cmd == "set_bot_level":
                if not is_host: error = "ホストのみ変更できます"
                else:
                    p = room.get_player(msg.get("pid",""))
                    if p and p.is_bot and msg.get("level") in ("easy","normal","hard"): p.level = msg["level"]
            elif cmd == "start":
                if not is_host: error = "ホストのみ開始できます"
                else:
                    try: room.start_game()
                    except ValueError as e: error = str(e)
            elif cmd == "action":
                if not room.game: error = "ゲームが始まっていません"
                elif not pid: error = "プレイヤーが見つかりません"
                else:
                    ok, text = room.game.act(pid, msg.get("action") or {})
                    if not ok: error = text
            elif cmd == "back_to_lobby":
                if not is_host: error = "ホストのみ戻せます"
                elif room.game and not room.game.finished: error = "ゲーム中です"
                else: room.game = None
            elif cmd == "rematch":
                if not is_host: error = "ホストのみ再戦できます"
                elif not room.game or not room.game.finished: error = "まだ終了していません"
                else:
                    try: room.start_game()
                    except ValueError as e: error = str(e)
            elif cmd == "chat":
                text = str(msg.get("text", ""))[:80].strip()
                if text:
                    for ws2 in list(room.sockets.values()):
                        try: await ws2.send_json({"type":"chat","pid":pid,"text":text})
                        except Exception: pass
            else:
                error = "不明な操作です"

            if error:
                await ws.send_json({"type":"error","message":error})
            await send_state(room)
            if cmd in ("start","action","rematch"):
                await schedule_bots(room)
    except WebSocketDisconnect:
        room.sockets.pop(token, None)
    except Exception:
        room.sockets.pop(token, None)


@app.get("/health")
def health():
    return {"ok": True, "rooms": len(rooms), "games": len(CATALOG)}


@app.get("/")
def root():
    return FileResponse(BASE / "index.html")


@app.get("/join/{code}")
def join_page(code: str):
    return FileResponse(BASE / "index.html")


@app.get("/{path:path}")
def spa(path: str):
    return FileResponse(BASE / "index.html")

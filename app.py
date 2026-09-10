from __future__ import annotations

import asyncio
import math
import random
import secrets
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import qrcode
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
app = FastAPI(title="BOUNCE TANKS")

W, H = 1000.0, 640.0
TANK_R, TANK_SPEED, TURN_SPEED = 20.0, 190.0, 2.8
BULLET_R, BULLET_SPEED = 6.0, 500.0
BULLET_LIFE, BULLET_BOUNCES, SHOT_CD = 5.5, 5, 0.42
MAX_BULLETS, MAX_PLAYERS = 3, 4
ITEM_KINDS = ("laser", "shotgun", "shield", "mine")
ITEM_ICONS = {"laser": "⚡", "shotgun": "🔱", "shield": "🛡️", "mine": "💣"}
COLORS = ["#64e5ff", "#ff6d8d", "#ffd15c", "#8cff8a"]
BOT_COLORS = ["#ff6d8d", "#ffd15c", "#b68cff"]
REACTIONS = {"🔥", "😂", "💥", "😭", "👏"}

@dataclass
class Player:
    id: str
    token: str
    name: str
    color: str
    x: float = 0
    y: float = 0
    a: float = 0
    alive: bool = True
    score: int = 0
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    fire: bool = False
    last_shot: float = 0
    held_item: Optional[str] = None
    shield_until: float = 0
    bot: bool = False
    ai_next: float = 0
    ai_turn_bias: float = 0
    ai_fire_at: float = 0

@dataclass
class Bullet:
    id: str
    owner: str
    x: float
    y: float
    vx: float
    vy: float
    born: float
    bounces: int = 0
    max_bounces: int = BULLET_BOUNCES
    life: float = BULLET_LIFE
    kind: str = "normal"

@dataclass
class Pickup:
    id: str
    kind: str
    x: float
    y: float

@dataclass
class Mine:
    id: str
    owner: str
    x: float
    y: float
    born: float

@dataclass
class Room:
    code: str
    host_token: str
    host_id: str
    target: int
    players: Dict[str, Player] = field(default_factory=dict)
    mode: str = "battle"
    items_enabled: bool = True
    stage: str = "lobby"
    round: int = 0
    walls: List[dict] = field(default_factory=list)
    bullets: List[Bullet] = field(default_factory=list)
    pickups: List[Pickup] = field(default_factory=list)
    mines: List[Mine] = field(default_factory=list)
    effects: List[dict] = field(default_factory=list)
    killfeed: List[dict] = field(default_factory=list)
    chat: List[dict] = field(default_factory=list)
    countdown_until: float = 0
    next_round_at: float = 0
    winner_id: Optional[str] = None
    round_winner_id: Optional[str] = None
    message: str = ""
    reactions: List[dict] = field(default_factory=list)
    last_item_spawn: float = 0
    survival_wave: int = 0
    survival_kills: int = 0
    survival_started_at: float = 0
    survival_finished_at: float = 0
    last_active: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None

rooms: Dict[str, Room] = {}
clients: Dict[str, List[dict]] = {}

class CreateRoom(BaseModel):
    name: str = "HOST"
    target_score: int = 5
    mode: str = "battle"

class JoinRoom(BaseModel):
    name: str

def origin(req: Request) -> str:
    proto = req.headers.get("x-forwarded-proto") or req.url.scheme
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or req.url.netloc
    return f"{proto}://{host}".rstrip("/")

def clean_name(s: str) -> str:
    s = " ".join((s or "").strip().split())
    if not s:
        raise HTTPException(400, "名前を入力してね")
    return s[:18]

def new_code() -> str:
    for _ in range(500):
        c = f"{random.randint(0, 9999):04d}"
        if c not in rooms:
            return c
    raise HTTPException(503, "部屋を作れませんでした")

def human_players(room: Room) -> List[Player]:
    return [p for p in room.players.values() if not p.bot]

def bot_players(room: Room) -> List[Player]:
    return [p for p in room.players.values() if p.bot]

def pstate(p: Player, now: float) -> dict:
    return {"id": p.id, "name": p.name, "color": p.color, "x": round(p.x, 2), "y": round(p.y, 2), "angle": round(p.a, 4), "alive": p.alive, "score": p.score, "item": p.held_item, "shield": p.shield_until > now, "bot": p.bot}

def survival_elapsed(room: Room, now: float) -> float:
    if not room.survival_started_at:
        return 0
    end = room.survival_finished_at or now
    return max(0, end - room.survival_started_at)

def state(room: Room, viewer: Optional[str], is_host: bool) -> dict:
    now = time.time()
    room.reactions = [r for r in room.reactions if r["until"] > now]
    room.effects = [e for e in room.effects if e["until"] > now]
    room.killfeed = [k for k in room.killfeed if k["until"] > now]
    room.chat = [c for c in room.chat if c["until"] > now]
    latest_chat = room.chat[-1] if room.chat else None
    return {
        "type": "state", "code": room.code, "stage": room.stage, "round": room.round,
        "mode": room.mode, "items_enabled": room.items_enabled,
        "target_score": room.target, "player_id": viewer, "is_host": is_host, "host_player_id": room.host_id,
        "players": [pstate(p, now) for p in room.players.values()], "walls": room.walls,
        "bullets": [{"id": b.id, "owner_id": b.owner, "x": round(b.x, 2), "y": round(b.y, 2), "kind": b.kind} for b in room.bullets],
        "pickups": [{"id": i.id, "kind": i.kind, "x": i.x, "y": i.y} for i in room.pickups],
        "mines": [{"id": m.id, "owner_id": m.owner, "x": m.x, "y": m.y, "armed": now - m.born >= 0.7} for m in room.mines],
        "effects": [{**{k: v for k, v in e.items() if k != "until"}, "ttl": max(0, e["until"] - now)} for e in room.effects],
        "killfeed": [{"text": k["text"], "ttl": max(0, k["until"] - now)} for k in room.killfeed],
        "chat": ({"name": latest_chat["name"], "text": latest_chat["text"], "ttl": max(0, latest_chat["until"] - now)} if latest_chat else None),
        "countdown": max(0, room.countdown_until - now) if room.stage == "countdown" else 0,
        "winner_id": room.winner_id, "round_winner_id": room.round_winner_id, "message": room.message,
        "reactions": [{"emoji": r["emoji"], "sender": r["sender"], "ttl": max(0, r["until"] - now)} for r in room.reactions],
        "survival": {"wave": room.survival_wave, "kills": room.survival_kills, "elapsed": round(survival_elapsed(room, now), 1)},
        "world": {"w": W, "h": H}
    }

async def broadcast(code: str):
    room = rooms.get(code)
    if not room:
        return
    bad = []
    for c in list(clients.get(code, [])):
        try:
            await c["ws"].send_json(state(room, c["player_id"], c["is_host"]))
        except Exception:
            bad.append(c)
    if bad:
        clients[code] = [c for c in clients.get(code, []) if c not in bad]

def hit_rect(cx: float, cy: float, radius: float, r: dict) -> bool:
    nx = min(max(cx, r["x"]), r["x"] + r["w"])
    ny = min(max(cy, r["y"]), r["y"] + r["h"])
    return (cx - nx) ** 2 + (cy - ny) ** 2 < radius ** 2

def tank_blocked(room: Room, x: float, y: float) -> bool:
    if x - TANK_R < 0 or x + TANK_R > W or y - TANK_R < 0 or y + TANK_R > H:
        return True
    return any(hit_rect(x, y, TANK_R, r) for r in room.walls)

def point_blocked(room: Room, x: float, y: float, radius: float = 22) -> bool:
    if x - radius < 20 or x + radius > W - 20 or y - radius < 20 or y + radius > H - 20:
        return True
    if any(hit_rect(x, y, radius, r) for r in room.walls):
        return True
    if any((p.x-x)**2 + (p.y-y)**2 < (TANK_R+radius+24)**2 for p in room.players.values() if p.alive):
        return True
    return False

def wall_seg(out: list, x1: float, y1: float, x2: float, y2: float, t: float = 14):
    if abs(x1 - x2) < 0.01:
        out.append({"x": x1-t/2, "y": min(y1,y2)-t/2, "w": t, "h": abs(y2-y1)+t})
    else:
        out.append({"x": min(x1,x2)-t/2, "y": y1-t/2, "w": abs(x2-x1)+t, "h": t})

def make_maze() -> List[dict]:
    cols, rows, margin = 6, 4, 28.0
    cw, ch = (W-2*margin)/cols, (H-2*margin)/rows
    seen = [[False]*cols for _ in range(rows)]
    vert = [[True]*(cols-1) for _ in range(rows)]
    hori = [[True]*cols for _ in range(rows-1)]
    start = (random.randrange(rows), random.randrange(cols))
    stack = [start]; seen[start[0]][start[1]] = True
    while stack:
        r, c = stack[-1]; opts = []
        if r > 0 and not seen[r-1][c]: opts.append((r-1,c,"u"))
        if r+1 < rows and not seen[r+1][c]: opts.append((r+1,c,"d"))
        if c > 0 and not seen[r][c-1]: opts.append((r,c-1,"l"))
        if c+1 < cols and not seen[r][c+1]: opts.append((r,c+1,"r"))
        if not opts:
            stack.pop(); continue
        nr, nc, d = random.choice(opts)
        if d == "u": hori[r-1][c] = False
        elif d == "d": hori[r][c] = False
        elif d == "l": vert[r][c-1] = False
        else: vert[r][c] = False
        seen[nr][nc] = True; stack.append((nr,nc))
    leftovers = [("v",r,c) for r in range(rows) for c in range(cols-1) if vert[r][c]] + [("h",r,c) for r in range(rows-1) for c in range(cols) if hori[r][c]]
    random.shuffle(leftovers)
    for kind, r, c in leftovers[:max(2, len(leftovers)//5)]:
        if kind == "v": vert[r][c] = False
        else: hori[r][c] = False
    out = []
    for r in range(rows):
        for c in range(cols-1):
            if vert[r][c]:
                x = margin+(c+1)*cw; wall_seg(out, x, margin+r*ch, x, margin+(r+1)*ch)
    for r in range(rows-1):
        for c in range(cols):
            if hori[r][c]:
                y = margin+(r+1)*ch; wall_seg(out, margin+c*cw, y, margin+(c+1)*cw, y)
    return out

def add_effect(room: Room, kind: str, duration: float = 0.7, **data):
    room.effects.append({"kind": kind, "until": time.time()+duration, **data})

def add_killfeed(room: Room, text: str):
    room.killfeed.append({"text": text, "until": time.time()+2.8})

def clear_combat(room: Room):
    room.bullets.clear(); room.pickups.clear(); room.mines.clear(); room.effects.clear(); room.killfeed.clear()

def battle_spawns() -> List[tuple]:
    return [(90,90,.35),(W-90,H-90,math.pi+.35),(W-90,90,math.pi-.35),(90,H-90,-.35)]

def reset_battle_round(room: Room):
    room.walls = make_maze(); clear_combat(room)
    room.last_item_spawn = time.time()+2.5
    for p, (x,y,a) in zip(human_players(room), battle_spawns()):
        p.x,p.y,p.a = x,y,a; p.alive = True
        p.up=p.down=p.left=p.right=p.fire=False; p.last_shot=0
        p.held_item=None; p.shield_until=0

def start_round(room: Room):
    room.round += 1; room.winner_id=None; room.round_winner_id=None; room.message=""
    reset_battle_round(room); room.stage="countdown"; room.countdown_until=time.time()+3.2

def begin_battle(room: Room):
    for p in human_players(room): p.score = 0
    room.round = 0; start_round(room)

def finish_round(room: Room, winner: Optional[Player]):
    room.stage="round_end"; room.round_winner_id=winner.id if winner else None
    if winner:
        winner.score += 1; room.message=f"{winner.name} WIN!"
        if winner.score >= room.target:
            room.stage="finished"; room.winner_id=winner.id; room.message=f"{winner.name} CHAMPION!"; return
    else:
        room.message="DRAW!"
    room.next_round_at=time.time()+2.8

def remove_bots(room: Room):
    for pid in [p.id for p in bot_players(room)]:
        room.players.pop(pid, None)

def safe_spawn(room: Room, preferred: List[tuple]) -> tuple:
    for x,y,a in preferred:
        if not point_blocked(room, x, y, 30):
            return x,y,a
    for _ in range(100):
        x=random.uniform(70,W-70); y=random.uniform(70,H-70); a=random.uniform(0,math.pi*2)
        if not point_blocked(room,x,y,30): return x,y,a
    return 90,90,0

def spawn_survival_wave(room: Room, first: bool = False):
    remove_bots(room)
    if first:
        room.survival_wave = 1
    else:
        room.survival_wave += 1
    clear_combat(room)
    human = human_players(room)[0]
    if first:
        room.walls = make_maze()
        human.x,human.y,human.a = 90,H/2,0
    human.alive=True; human.up=human.down=human.left=human.right=human.fire=False
    human.held_item=None; human.shield_until=0; human.last_shot=0
    count = min(3, 1 + (room.survival_wave-1)//2)
    preferred = [(W-90,90,math.pi),(W-90,H-90,math.pi),(W/2,H-90,-math.pi/2)]
    for i in range(count):
        x,y,a=safe_spawn(room, preferred[i:]+preferred[:i])
        pid=f"bot-{secrets.token_hex(5)}"
        bot=Player(pid,"",f"CPU-{i+1}",BOT_COLORS[i%len(BOT_COLORS)],x,y,a,bot=True)
        bot.ai_next=time.time()+random.uniform(.2,.8); bot.ai_fire_at=time.time()+random.uniform(.7,1.5)
        room.players[pid]=bot
    room.last_item_spawn=time.time()+2.0
    room.stage="countdown"; room.countdown_until=time.time()+(3.2 if first else 2.4)
    room.message=f"WAVE {room.survival_wave}"

def begin_survival(room: Room):
    room.survival_kills=0; room.survival_wave=0; room.survival_started_at=time.time(); room.survival_finished_at=0
    room.round=0; room.winner_id=None; room.round_winner_id=None
    spawn_survival_wave(room, first=True)

def finish_survival(room: Room):
    if room.stage == "finished": return
    room.survival_finished_at=time.time(); room.stage="finished"; room.message="GAME OVER"
    room.bullets.clear(); room.pickups.clear(); room.mines.clear()

def kill_player(room: Room, victim: Player, attacker_id: Optional[str], source: str, x: float, y: float, now: float) -> bool:
    if not victim.alive: return False
    if victim.shield_until > now:
        victim.shield_until=0; add_effect(room,"shield_break",.55,x=victim.x,y=victim.y); return False
    victim.alive=False; victim.up=victim.down=victim.left=victim.right=victim.fire=False
    add_effect(room,"explosion",.75,x=x,y=y,color=victim.color)
    attacker=room.players.get(attacker_id or "")
    if attacker and attacker.id != victim.id: add_killfeed(room,f"{attacker.name} → {victim.name}  {source}")
    elif attacker and attacker.id == victim.id: add_killfeed(room,f"{victim.name} 自爆！")
    else: add_killfeed(room,f"{victim.name} 撃破")
    if room.mode == "survival" and victim.bot and attacker and not attacker.bot:
        room.survival_kills += 1
    return True

def shoot(room: Room, p: Player, now: float):
    if room.stage != "playing" or not p.alive or now-p.last_shot < SHOT_CD: return
    if sum(1 for b in room.bullets if b.owner==p.id and b.kind=="normal") >= MAX_BULLETS: return
    p.last_shot=now; muzzle=TANK_R+10
    speed=BULLET_SPEED*((1+min(.18, room.survival_wave*.012)) if p.bot and room.mode=="survival" else 1)
    room.bullets.append(Bullet(secrets.token_hex(5),p.id,p.x+math.cos(p.a)*muzzle,p.y+math.sin(p.a)*muzzle,math.cos(p.a)*speed,math.sin(p.a)*speed,now))

def spawn_item(room: Room, now: float):
    if not room.items_enabled or room.stage != "playing" or len(room.pickups)>=2 or now<room.last_item_spawn: return
    room.last_item_spawn=now+random.uniform(5.5,8.0)
    for _ in range(80):
        x=random.uniform(70,W-70); y=random.uniform(70,H-70)
        if not point_blocked(room,x,y,24):
            room.pickups.append(Pickup(secrets.token_hex(4),random.choice(ITEM_KINDS),round(x,1),round(y,1))); return

def use_laser(room: Room, p: Player, now: float):
    step=8.0; x,y=p.x+math.cos(p.a)*30,p.y+math.sin(p.a)*30; ex,ey=x,y; hit=None
    for _ in range(170):
        nx,ny=ex+math.cos(p.a)*step,ey+math.sin(p.a)*step
        if nx<5 or nx>W-5 or ny<5 or ny>H-5 or any(hit_rect(nx,ny,3,r) for r in room.walls): break
        ex,ey=nx,ny
        for target in room.players.values():
            if not target.alive or target.id==p.id: continue
            if room.mode=="survival" and p.bot and target.bot: continue
            if (target.x-ex)**2+(target.y-ey)**2 <= (TANK_R+7)**2:
                hit=target; break
        if hit: break
    add_effect(room,"laser",.38,x1=x,y1=y,x2=ex,y2=ey,color=p.color)
    if hit: kill_player(room,hit,p.id,"⚡",hit.x,hit.y,now)

def use_shotgun(room: Room, p: Player, now: float):
    muzzle=TANK_R+10
    for off in (-.28,-.14,0,.14,.28):
        a=p.a+off
        room.bullets.append(Bullet(secrets.token_hex(5),p.id,p.x+math.cos(a)*muzzle,p.y+math.sin(a)*muzzle,math.cos(a)*465,math.sin(a)*465,now,max_bounces=2,life=3.6,kind="shotgun"))
    add_effect(room,"muzzle",.22,x=p.x+math.cos(p.a)*34,y=p.y+math.sin(p.a)*34,color=p.color)

def use_mine(room: Room, p: Player, now: float):
    x=p.x-math.cos(p.a)*30; y=p.y-math.sin(p.a)*30
    if any(hit_rect(x,y,13,r) for r in room.walls): x,y=p.x,p.y
    room.mines.append(Mine(secrets.token_hex(5),p.id,x,y,now))

def use_item(room: Room, p: Player, now: float):
    if not room.items_enabled or room.stage!="playing" or not p.alive or not p.held_item: return
    kind=p.held_item; p.held_item=None
    if kind=="laser": use_laser(room,p,now)
    elif kind=="shotgun": use_shotgun(room,p,now)
    elif kind=="shield":
        p.shield_until=now+7.0; add_effect(room,"shield_on",.55,x=p.x,y=p.y,color=p.color)
    elif kind=="mine": use_mine(room,p,now)

def angle_diff(target: float, current: float) -> float:
    return (target-current+math.pi)%(2*math.pi)-math.pi

def update_bot(room: Room, p: Player, dt: float, now: float):
    human=next((h for h in human_players(room) if h.alive),None)
    if not human or not p.alive: return
    desired=math.atan2(human.y-p.y,human.x-p.x)
    if now>=p.ai_next:
        jitter=max(.08,.48-room.survival_wave*.025)
        p.ai_turn_bias=random.uniform(-jitter,jitter)
        p.ai_next=now+random.uniform(.45,1.0)
    target=desired+p.ai_turn_bias
    diff=angle_diff(target,p.a)
    turn=1 if diff>.08 else -1 if diff<-.08 else 0
    p.a=(p.a+turn*TURN_SPEED*.72*dt)%(2*math.pi)
    speed=TANK_SPEED*(.67+min(.2,room.survival_wave*.018))
    dx,dy=math.cos(p.a)*speed*dt,math.sin(p.a)*speed*dt
    moved=False
    if not tank_blocked(room,p.x+dx,p.y): p.x+=dx; moved=True
    if not tank_blocked(room,p.x,p.y+dy): p.y+=dy; moved=True
    if not moved:
        p.a=(p.a+random.choice([-1,1])*random.uniform(.9,1.7))%(2*math.pi)
        p.ai_next=now+random.uniform(.25,.55)
    aim=abs(angle_diff(desired,p.a))
    fire_gap=max(.62,1.4-room.survival_wave*.055)
    if now>=p.ai_fire_at and aim<.42:
        shoot(room,p,now); p.ai_fire_at=now+random.uniform(fire_gap,fire_gap+.65)

def update_player(room: Room, p: Player, dt: float, now: float):
    if not p.alive: return
    if p.bot:
        update_bot(room,p,dt,now); return
    turn=(1 if p.right else 0)-(1 if p.left else 0); p.a=(p.a+turn*TURN_SPEED*dt)%(math.pi*2)
    move=(1 if p.up else 0)-(1 if p.down else 0); speed=TANK_SPEED*(.72 if move<0 else 1)
    dx,dy=math.cos(p.a)*speed*move*dt,math.sin(p.a)*speed*move*dt
    if not tank_blocked(room,p.x+dx,p.y): p.x+=dx
    if not tank_blocked(room,p.x,p.y+dy): p.y+=dy
    if p.fire: shoot(room,p,now)
    if room.items_enabled and not p.held_item:
        for item in list(room.pickups):
            if (p.x-item.x)**2+(p.y-item.y)**2 <= (TANK_R+19)**2:
                p.held_item=item.kind; room.pickups.remove(item); add_effect(room,"pickup",.5,x=p.x,y=p.y,text=ITEM_ICONS[item.kind],color=p.color); break

def update_bullets(room: Room, dt: float, now: float):
    keep=[]; steps=3; sdt=dt/steps
    for b in room.bullets:
        gone=False; owner=room.players.get(b.owner)
        for _ in range(steps):
            nx=b.x+b.vx*sdt; hitx=nx-BULLET_R<=0 or nx+BULLET_R>=W or any(hit_rect(nx,b.y,BULLET_R,r) for r in room.walls)
            if hitx: b.vx*=-1; b.bounces+=1
            else: b.x=nx
            ny=b.y+b.vy*sdt; hity=ny-BULLET_R<=0 or ny+BULLET_R>=H or any(hit_rect(b.x,ny,BULLET_R,r) for r in room.walls)
            if hity: b.vy*=-1; b.bounces+=1
            else: b.y=ny
            if b.bounces>b.max_bounces: gone=True; break
            age=now-b.born
            for p in room.players.values():
                if not p.alive or (p.id==b.owner and age<.25): continue
                if room.mode=="survival" and owner and owner.bot and p.bot: continue
                if (p.x-b.x)**2+(p.y-b.y)**2 <= (TANK_R+BULLET_R-2)**2:
                    kill_player(room,p,b.owner,"💥",p.x,p.y,now); gone=True; break
            if gone: break
        if not gone and now-b.born<=b.life: keep.append(b)
    room.bullets=keep

def update_mines(room: Room, now: float):
    keep=[]
    for m in room.mines:
        age=now-m.born
        if age>18: continue
        triggered=False; owner=room.players.get(m.owner)
        if age>=.7:
            for p in room.players.values():
                if not p.alive or (p.id==m.owner and age<1.4): continue
                if room.mode=="survival" and owner and owner.bot and p.bot: continue
                if (p.x-m.x)**2+(p.y-m.y)**2 <= 31**2:
                    add_effect(room,"explosion",.75,x=m.x,y=m.y,color="#ffb34a"); kill_player(room,p,m.owner,"💣",p.x,p.y,now); triggered=True; break
        if not triggered: keep.append(m)
    room.mines=keep

def update_survival_state(room: Room, now: float):
    human=human_players(room)[0] if human_players(room) else None
    if not human or not human.alive:
        finish_survival(room); return
    alive_bots=[p for p in bot_players(room) if p.alive]
    if not alive_bots:
        room.stage="wave_clear"; room.message=f"WAVE {room.survival_wave} CLEAR!"; room.next_round_at=now+2.0
        room.bullets.clear(); room.mines.clear(); room.pickups.clear()

async def game_loop(code: str):
    try:
        last=time.perf_counter(); acc=0.0
        while code in rooms:
            room=rooms.get(code)
            if not room: return
            t=time.perf_counter(); dt=min(.05,t-last); last=t; now=time.time()
            if room.stage=="countdown" and now>=room.countdown_until:
                room.stage="playing"; room.message="GO!"
            elif room.stage=="playing":
                for p in list(room.players.values()): update_player(room,p,dt,now)
                update_bullets(room,dt,now); update_mines(room,now); spawn_item(room,now)
                if room.mode=="survival": update_survival_state(room,now)
                else:
                    alive=[p for p in human_players(room) if p.alive]
                    if len(human_players(room))>=2 and len(alive)<=1: finish_round(room,alive[0] if alive else None)
            elif room.stage=="round_end" and now>=room.next_round_at:
                start_round(room)
            elif room.stage=="wave_clear" and now>=room.next_round_at:
                spawn_survival_wave(room,first=False)
            acc+=dt
            if acc>=.05:
                acc=0; await broadcast(code)
            await asyncio.sleep(max(0,1/30-(time.perf_counter()-t)))
    except asyncio.CancelledError:
        pass

def ensure_task(room: Room):
    if room.task is None or room.task.done(): room.task=asyncio.create_task(game_loop(room.code))

@app.get("/", response_class=HTMLResponse)
@app.get("/host/{code}", response_class=HTMLResponse)
@app.get("/join/{code}", response_class=HTMLResponse)
async def page(code: Optional[str]=None):
    return HTMLResponse(HTML, headers={"Cache-Control":"no-store"})

@app.post("/api/rooms")
async def create_room(data: CreateRoom, req: Request):
    name=clean_name(data.name); target=data.target_score if data.target_score in (3,5,7) else 5
    mode=data.mode if data.mode in ("battle","survival") else "battle"
    code=new_code(); host_token=secrets.token_urlsafe(24); pid=secrets.token_hex(8)
    host=Player(pid,secrets.token_urlsafe(20),name,COLORS[0])
    room=Room(code,host_token,pid,target,{pid:host},mode=mode,items_enabled=True)
    rooms[code]=room; clients[code]=[]; ensure_task(room)
    o=origin(req)
    return {"code":code,"host_url":f"{o}/host/{code}#token={host_token}","join_url":f"{o}/join/{code}","mode":mode}

@app.get("/api/rooms/{code}")
async def room_info(code: str):
    room=rooms.get(code)
    if not room: raise HTTPException(404,"部屋が見つかりません")
    return {"code":code,"stage":room.stage,"players":len(human_players(room)),"max_players":MAX_PLAYERS,"mode":room.mode}

@app.post("/api/rooms/{code}/join")
async def join_room(code: str, data: JoinRoom):
    room=rooms.get(code)
    if not room: raise HTTPException(404,"部屋が見つかりません")
    if room.mode!="battle": raise HTTPException(409,"この部屋は1人用です")
    if room.stage!="lobby": raise HTTPException(409,"ゲーム中の部屋には参加できません")
    if len(human_players(room))>=MAX_PLAYERS: raise HTTPException(409,"この部屋は満員です")
    name=clean_name(data.name)
    if any(p.name.lower()==name.lower() for p in human_players(room)): raise HTTPException(409,"同じ名前の人がいます")
    pid=secrets.token_hex(8); token=secrets.token_urlsafe(20); color=COLORS[len(human_players(room))%len(COLORS)]
    room.players[pid]=Player(pid,token,name,color); room.last_active=time.time(); await broadcast(code)
    return {"player_id":pid,"player_token":token,"name":name}

@app.get("/room/{code}/qr")
async def qr(code: str, req: Request):
    room=rooms.get(code)
    if not room: raise HTTPException(404,"部屋が見つかりません")
    if room.mode!="battle": raise HTTPException(404,"1人用にはQRコードはありません")
    qr_code=qrcode.QRCode(version=None,error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=8,border=3)
    qr_code.add_data(f"{origin(req)}/join/{code}"); qr_code.make(fit=True)
    img=qr_code.make_image(fill_color="black",back_color="white"); bio=BytesIO(); img.save(bio,format="PNG"); bio.seek(0)
    return StreamingResponse(bio,media_type="image/png",headers={"Cache-Control":"private, max-age=3600","Content-Disposition":f'inline; filename="room-{code}-qr.png"'})

@app.websocket("/ws/{code}")
async def ws_room(ws: WebSocket, code: str, role: str, token: str, player_id: Optional[str]=None):
    room=rooms.get(code)
    if not room: await ws.close(code=4404); return
    is_host=role=="host"; viewer=None
    if is_host:
        if token!=room.host_token: await ws.close(code=4403); return
        viewer=room.host_id
    else:
        p=room.players.get(player_id or "")
        if not p or p.bot or p.token!=token: await ws.close(code=4403); return
        viewer=p.id
    await ws.accept(); conn={"ws":ws,"player_id":viewer,"is_host":is_host}; clients.setdefault(code,[]).append(conn); ensure_task(room); await ws.send_json(state(room,viewer,is_host))
    try:
        while True:
            msg=await ws.receive_json(); room=rooms.get(code)
            if not room: break
            room.last_active=time.time(); action=msg.get("action"); p=room.players.get(viewer or "")
            if action=="input" and p and not p.bot:
                k=msg.get("keys") or {}; p.up=bool(k.get("up")); p.down=bool(k.get("down")); p.left=bool(k.get("left")); p.right=bool(k.get("right")); p.fire=bool(k.get("shoot"))
            elif action=="shoot" and p and not p.bot:
                shoot(room,p,time.time())
            elif action=="use_item" and p and not p.bot:
                use_item(room,p,time.time()); await broadcast(code)
            elif action=="set_items":
                if not is_host or room.stage!="lobby": continue
                room.items_enabled=bool(msg.get("enabled")); await broadcast(code)
            elif action=="start":
                if not is_host: await ws.send_json({"type":"error","message":"ホストだけが開始できます"}); continue
                if room.stage not in ("lobby","finished"): continue
                if room.mode=="battle":
                    if len(human_players(room))<2: await ws.send_json({"type":"error","message":"2人以上で開始してね"}); continue
                    remove_bots(room); begin_battle(room)
                else:
                    remove_bots(room); begin_survival(room)
                await broadcast(code)
            elif action=="reset" and is_host:
                remove_bots(room); room.stage="lobby"; room.round=0; room.walls=[]; clear_combat(room); room.chat.clear()
                room.winner_id=None; room.round_winner_id=None; room.message=""; room.survival_wave=0; room.survival_kills=0; room.survival_started_at=0; room.survival_finished_at=0
                for pp in human_players(room):
                    pp.score=0; pp.alive=True; pp.held_item=None; pp.shield_until=0; pp.up=pp.down=pp.left=pp.right=pp.fire=False
                await broadcast(code)
            elif action=="reaction" and p and not p.bot and msg.get("emoji") in REACTIONS:
                room.reactions.append({"emoji":msg["emoji"],"sender":p.name,"until":time.time()+2}); await broadcast(code)
            elif action=="chat" and p and not p.bot and room.mode=="battle":
                text=" ".join(str(msg.get("text") or "").strip().split())[:60]
                if text:
                    room.chat.append({"name":p.name,"text":text,"until":time.time()+7}); room.chat=room.chat[-8:]; await broadcast(code)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        clients[code]=[c for c in clients.get(code,[]) if c["ws"] is not ws]
        room=rooms.get(code)
        if room and viewer in room.players:
            p=room.players[viewer]; p.up=p.down=p.left=p.right=p.fire=False

@app.on_event("startup")
async def cleanup():
    async def loop():
        while True:
            await asyncio.sleep(1800); now=time.time()
            for code in [c for c,r in rooms.items() if now-r.last_active>12*3600]:
                r=rooms.pop(code,None)
                if r and r.task: r.task.cancel()
                clients.pop(code,None)
    asyncio.create_task(loop())

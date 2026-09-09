from __future__ import annotations

import asyncio, math, random, secrets, time
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
COLORS = ["#64e5ff", "#ff6d8d", "#ffd15c", "#8cff8a"]
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

@dataclass
class Room:
    code: str
    host_token: str
    host_id: str
    target: int
    players: Dict[str, Player] = field(default_factory=dict)
    stage: str = "lobby"
    round: int = 0
    walls: List[dict] = field(default_factory=list)
    bullets: List[Bullet] = field(default_factory=list)
    countdown_until: float = 0
    next_round_at: float = 0
    winner_id: Optional[str] = None
    round_winner_id: Optional[str] = None
    message: str = ""
    reactions: List[dict] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None

rooms: Dict[str, Room] = {}
clients: Dict[str, List[dict]] = {}

class CreateRoom(BaseModel):
    name: str = "HOST"
    target_score: int = 5

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
        c = f"{random.randint(0,9999):04d}"
        if c not in rooms:
            return c
    raise HTTPException(503, "部屋を作れませんでした")

def pstate(p: Player) -> dict:
    return {"id":p.id,"name":p.name,"color":p.color,"x":round(p.x,2),"y":round(p.y,2),"angle":round(p.a,4),"alive":p.alive,"score":p.score}

def state(room: Room, viewer: Optional[str], is_host: bool) -> dict:
    now = time.time()
    room.reactions = [r for r in room.reactions if r["until"] > now]
    return {
        "type":"state","code":room.code,"stage":room.stage,"round":room.round,
        "target_score":room.target,"player_id":viewer,"is_host":is_host,"host_player_id":room.host_id,
        "players":[pstate(p) for p in room.players.values()],"walls":room.walls,
        "bullets":[{"id":b.id,"owner_id":b.owner,"x":round(b.x,2),"y":round(b.y,2)} for b in room.bullets],
        "countdown":max(0,room.countdown_until-now) if room.stage=="countdown" else 0,
        "winner_id":room.winner_id,"round_winner_id":room.round_winner_id,"message":room.message,
        "reactions":[{"emoji":r["emoji"],"sender":r["sender"],"ttl":max(0,r["until"]-now)} for r in room.reactions],
        "world":{"w":W,"h":H}
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
    return (cx-nx)**2 + (cy-ny)**2 < radius**2

def tank_blocked(room: Room, x: float, y: float) -> bool:
    if x-TANK_R < 0 or x+TANK_R > W or y-TANK_R < 0 or y+TANK_R > H:
        return True
    return any(hit_rect(x,y,TANK_R,r) for r in room.walls)

def wall_seg(out: list, x1: float, y1: float, x2: float, y2: float, t: float=14):
    if abs(x1-x2) < 0.01:
        out.append({"x":x1-t/2,"y":min(y1,y2)-t/2,"w":t,"h":abs(y2-y1)+t})
    else:
        out.append({"x":min(x1,x2)-t/2,"y":y1-t/2,"w":abs(x2-x1)+t,"h":t})

def make_maze() -> List[dict]:
    cols, rows, margin = 6, 4, 28.0
    cw, ch = (W-2*margin)/cols, (H-2*margin)/rows
    seen = [[False]*cols for _ in range(rows)]
    vert = [[True]*(cols-1) for _ in range(rows)]
    hori = [[True]*cols for _ in range(rows-1)]
    start = (random.randrange(rows), random.randrange(cols))
    stack = [start]; seen[start[0]][start[1]] = True
    while stack:
        r,c = stack[-1]; opts=[]
        if r>0 and not seen[r-1][c]: opts.append((r-1,c,"u"))
        if r+1<rows and not seen[r+1][c]: opts.append((r+1,c,"d"))
        if c>0 and not seen[r][c-1]: opts.append((r,c-1,"l"))
        if c+1<cols and not seen[r][c+1]: opts.append((r,c+1,"r"))
        if not opts:
            stack.pop(); continue
        nr,nc,d = random.choice(opts)
        if d=="u": hori[r-1][c]=False
        elif d=="d": hori[r][c]=False
        elif d=="l": vert[r][c-1]=False
        else: vert[r][c]=False
        seen[nr][nc]=True; stack.append((nr,nc))
    leftovers = [("v",r,c) for r in range(rows) for c in range(cols-1) if vert[r][c]] + [("h",r,c) for r in range(rows-1) for c in range(cols) if hori[r][c]]
    random.shuffle(leftovers)
    for kind,r,c in leftovers[:max(2,len(leftovers)//5)]:
        if kind=="v": vert[r][c]=False
        else: hori[r][c]=False
    out=[]
    for r in range(rows):
        for c in range(cols-1):
            if vert[r][c]:
                x=margin+(c+1)*cw; wall_seg(out,x,margin+r*ch,x,margin+(r+1)*ch)
    for r in range(rows-1):
        for c in range(cols):
            if hori[r][c]:
                y=margin+(r+1)*ch; wall_seg(out,margin+c*cw,y,margin+(c+1)*cw,y)
    return out

def reset_round(room: Room):
    room.walls = make_maze(); room.bullets.clear()
    pts=[(90,90,.35),(W-90,H-90,math.pi+.35),(W-90,90,math.pi-.35),(90,H-90,-.35)]
    for p,(x,y,a) in zip(room.players.values(),pts):
        p.x,p.y,p.a=x,y,a; p.alive=True
        p.up=p.down=p.left=p.right=p.fire=False; p.last_shot=0

def start_round(room: Room):
    room.round += 1; room.winner_id=None; room.round_winner_id=None; room.message=""
    reset_round(room); room.stage="countdown"; room.countdown_until=time.time()+3.2

def begin_game(room: Room):
    for p in room.players.values(): p.score=0
    room.round=0; start_round(room)

def finish_round(room: Room, winner: Optional[Player]):
    room.stage="round_end"; room.round_winner_id=winner.id if winner else None
    if winner:
        winner.score += 1; room.message=f"{winner.name} WIN!"
        if winner.score >= room.target:
            room.stage="finished"; room.winner_id=winner.id; room.message=f"{winner.name} CHAMPION!"; return
    else:
        room.message="DRAW!"
    room.next_round_at=time.time()+2.7

def shoot(room: Room, p: Player, now: float):
    if room.stage!="playing" or not p.alive or now-p.last_shot < SHOT_CD:
        return
    if sum(1 for b in room.bullets if b.owner==p.id) >= MAX_BULLETS:
        return
    p.last_shot=now
    muzzle=TANK_R+10
    room.bullets.append(Bullet(secrets.token_hex(5),p.id,p.x+math.cos(p.a)*muzzle,p.y+math.sin(p.a)*muzzle,math.cos(p.a)*BULLET_SPEED,math.sin(p.a)*BULLET_SPEED,now))

def update_player(room: Room, p: Player, dt: float, now: float):
    if not p.alive: return
    turn = (1 if p.right else 0) - (1 if p.left else 0)
    p.a = (p.a + turn*TURN_SPEED*dt) % (math.pi*2)
    move = (1 if p.up else 0) - (1 if p.down else 0)
    speed = TANK_SPEED*(0.72 if move<0 else 1)
    dx,dy = math.cos(p.a)*speed*move*dt, math.sin(p.a)*speed*move*dt
    if not tank_blocked(room,p.x+dx,p.y): p.x += dx
    if not tank_blocked(room,p.x,p.y+dy): p.y += dy
    if p.fire: shoot(room,p,now)

def update_bullets(room: Room, dt: float, now: float):
    keep=[]; steps=3; sdt=dt/steps
    for b in room.bullets:
        gone=False
        for _ in range(steps):
            nx=b.x+b.vx*sdt
            hitx = nx-BULLET_R<=0 or nx+BULLET_R>=W or any(hit_rect(nx,b.y,BULLET_R,r) for r in room.walls)
            if hitx: b.vx*=-1; b.bounces+=1
            else: b.x=nx
            ny=b.y+b.vy*sdt
            hity = ny-BULLET_R<=0 or ny+BULLET_R>=H or any(hit_rect(b.x,ny,BULLET_R,r) for r in room.walls)
            if hity: b.vy*=-1; b.bounces+=1
            else: b.y=ny
            if b.bounces>BULLET_BOUNCES: gone=True; break
            age=now-b.born
            for p in room.players.values():
                if not p.alive or (p.id==b.owner and age<.25): continue
                if (p.x-b.x)**2+(p.y-b.y)**2 <= (TANK_R+BULLET_R-2)**2:
                    p.alive=False; gone=True; break
            if gone: break
        if not gone and now-b.born<=BULLET_LIFE: keep.append(b)
    room.bullets=keep

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
                for p in room.players.values(): update_player(room,p,dt,now)
                update_bullets(room,dt,now)
                alive=[p for p in room.players.values() if p.alive]
                if len(room.players)>=2 and len(alive)<=1: finish_round(room,alive[0] if alive else None)
            elif room.stage=="round_end" and now>=room.next_round_at:
                start_round(room)
            acc += dt
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
    return HTMLResponse(HTML)

@app.post("/api/rooms")
async def create_room(data: CreateRoom, req: Request):
    name=clean_name(data.name); target=data.target_score if data.target_score in (3,5,7) else 5
    code=new_code(); host_token=secrets.token_urlsafe(24); pid=secrets.token_hex(8)
    host=Player(pid,secrets.token_urlsafe(20),name,COLORS[0])
    room=Room(code,host_token,pid,target,{pid:host}); rooms[code]=room; clients[code]=[]; ensure_task(room)
    o=origin(req)
    return {"code":code,"host_url":f"{o}/host/{code}#token={host_token}","join_url":f"{o}/join/{code}"}

@app.get("/api/rooms/{code}")
async def room_info(code: str):
    room=rooms.get(code)
    if not room: raise HTTPException(404,"部屋が見つかりません")
    return {"code":code,"stage":room.stage,"players":len(room.players),"max_players":MAX_PLAYERS}

@app.post("/api/rooms/{code}/join")
async def join_room(code: str, data: JoinRoom):
    room=rooms.get(code)
    if not room: raise HTTPException(404,"部屋が見つかりません")
    if room.stage!="lobby": raise HTTPException(409,"ゲーム中の部屋には参加できません")
    if len(room.players)>=MAX_PLAYERS: raise HTTPException(409,"この部屋は満員です")
    name=clean_name(data.name)
    if any(p.name.lower()==name.lower() for p in room.players.values()): raise HTTPException(409,"同じ名前の人がいます")
    pid=secrets.token_hex(8); token=secrets.token_urlsafe(20); color=COLORS[len(room.players)%len(COLORS)]
    room.players[pid]=Player(pid,token,name,color); room.last_active=time.time(); await broadcast(code)
    return {"player_id":pid,"player_token":token,"name":name}

@app.get("/room/{code}/qr")
async def qr(code: str, req: Request):
    if code not in rooms: raise HTTPException(404,"部屋が見つかりません")
    img=qrcode.make(f"{origin(req)}/join/{code}"); bio=BytesIO(); img.save(bio,format="PNG"); bio.seek(0)
    return StreamingResponse(bio,media_type="image/png",headers={"Cache-Control":"no-store"})

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
        if not p or p.token!=token: await ws.close(code=4403); return
        viewer=p.id
    await ws.accept(); conn={"ws":ws,"player_id":viewer,"is_host":is_host}; clients.setdefault(code,[]).append(conn); ensure_task(room)
    await ws.send_json(state(room,viewer,is_host))
    try:
        while True:
            msg=await ws.receive_json(); room=rooms.get(code)
            if not room: break
            room.last_active=time.time(); action=msg.get("action"); p=room.players.get(viewer or "")
            if action=="input" and p:
                k=msg.get("keys") or {}; p.up=bool(k.get("up")); p.down=bool(k.get("down")); p.left=bool(k.get("left")); p.right=bool(k.get("right")); p.fire=bool(k.get("shoot"))
            elif action=="shoot" and p: shoot(room,p,time.time())
            elif action=="start":
                if not is_host: await ws.send_json({"type":"error","message":"ホストだけが開始できます"}); continue
                if room.stage not in ("lobby","finished"): continue
                if len(room.players)<2: await ws.send_json({"type":"error","message":"2人以上で開始してね"}); continue
                begin_game(room); await broadcast(code)
            elif action=="reset" and is_host:
                room.stage="lobby"; room.round=0; room.walls=[]; room.bullets=[]; room.winner_id=None; room.round_winner_id=None; room.message=""
                for pp in room.players.values(): pp.score=0; pp.alive=True
                await broadcast(code)
            elif action=="reaction" and p and msg.get("emoji") in REACTIONS:
                room.reactions.append({"emoji":msg["emoji"],"sender":p.name,"until":time.time()+2}); await broadcast(code)
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

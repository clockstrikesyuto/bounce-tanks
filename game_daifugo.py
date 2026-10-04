from game_core import *

class DaifugoGame(BaseGame):
    id, name, min_players, max_players, recommended = "daifugo", "大富豪", 3, 6, 4

    def start(self):
        deck = make_deck(1)
        self.rng.shuffle(deck)
        for i, c in enumerate(deck):
            self.hands[self.players[i % len(self.players)].id].append(c)
        for k in self.hands:
            self.hands[k] = sort_hand(self.hands[k])
        self.pile: List[Dict[str, Any]] = []
        self.last_play: Optional[Tuple[int, int, str]] = None
        self.passes = set()
        self.revolution = False
        holder = next((i for i,p in enumerate(self.players) if any(c["id"] == "D3" for c in self.hands[p.id])), 0)
        self.turn = holder
        self.log.append("大富豪スタート")

    def _strength(self, rank: int) -> int:
        if rank == 99: return 100
        return -rank if self.revolution else rank

    def _legal_groups(self, pid: str) -> List[List[str]]:
        hand = self.hands[pid]; by = defaultdict(list); jokers = []
        for c in hand: (jokers if c["suit"] == "J" else by[c["rank"]]).append(c["id"])
        groups = []; need = self.last_play[1] if self.last_play else None
        for rank, ids in by.items():
            for n in range(1, len(ids)+1):
                if need and n != need: continue
                if not self.last_play or self._strength(rank) > self._strength(self.last_play[0]): groups.append(ids[:n])
                if jokers and n+1 <= len(ids)+1 and (not need or n+1 == need):
                    if not self.last_play or self._strength(99) > self._strength(self.last_play[0]): groups.append(ids[:n] + [jokers[0]])
        if jokers and (not need or need == 1): groups.append([jokers[0]])
        return groups

    def actions(self, pid):
        if pid != self.current_pid or self.finished: return []
        acts = [{"type":"play","label":"出す","select":{"min":1,"max":4,"eligible":[c["id"] for c in self.hands[pid]]}}]
        if self.last_play: acts.append({"type":"pass","label":"パス"})
        return acts

    def act(self, pid, action):
        if pid != self.current_pid: return False, "あなたの手番ではありません"
        typ = action.get("type")
        if typ == "pass":
            if not self.last_play: return False, "場が空いている時はパスできません"
            self.passes.add(pid); self.log.append(f"{pid} パス")
            active = [p.id for p in self.players if self.hands[p.id]]
            if len(self.passes) >= max(1, len(active)-1):
                self.last_play = None; self.passes.clear(); self.pile.clear()
            self.next_turn()
            while self.hands.get(self.current_pid) == [] and not self.finished: self.next_turn()
            return True, "OK"
        if typ != "play": return False, "不正な操作"
        ids = action.get("cards", [])
        if not ids or len(ids) > 4: return False, "カードを選んでください"
        if any(card_by_id(self.hands[pid], cid) is None for cid in ids): return False, "そのカードは持っていません"
        cards = [card_by_id(self.hands[pid], cid) for cid in ids]
        nonj = [c for c in cards if c["suit"] != "J"]; ranks = set(c["rank"] for c in nonj)
        if len(ranks) > 1: return False, "同じ数字の組み合わせで出してください"
        rank = next(iter(ranks), 99)
        if self.last_play:
            if len(ids) != self.last_play[1]: return False, "場と同じ枚数で出してください"
            if self._strength(rank if rank != 99 else 99) <= self._strength(self.last_play[0]): return False, "場より強いカードを出してください"
        for cid in ids: self.hands[pid].remove(card_by_id(self.hands[pid], cid))
        self.pile = cards; self.last_play = (rank if rank != 99 else 99, len(ids), pid); self.passes.clear()
        self.log.append(f"{pid} が {','.join(c['label'] for c in cards)}")
        if len(ids) >= 4 and self.rules.get("revolution", True):
            self.revolution = not self.revolution; self.log.append("🔥 革命！")
        if any(c["rank"] == 8 for c in nonj) and self.rules.get("eightCut", True):
            self.last_play = None; self.pile = []; self.passes.clear(); self.log.append("8切り")
        if not self.hands[pid]:
            self.winners.append(pid)
            if len(self.winners) >= len(self.players)-1:
                self.winners += [p.id for p in self.players if p.id not in self.winners]
                self.finished = True; self.phase = "result"
                for i,w in enumerate(self.winners): self.scores[w] = len(self.players)-i
                return True, "ゲーム終了"
        self.next_turn()
        while self.hands.get(self.current_pid) == [] and not self.finished: self.next_turn()
        return True, "OK"

    def table_view(self, pid):
        return {"text": "革命中" if self.revolution else "通常", "cards": self.pile, "slots": []}

    def bot_action(self, pid, level="normal"):
        if pid != self.current_pid: return None
        groups = self._legal_groups(pid)
        if groups:
            if level == "easy": g = self.rng.choice(groups)
            else:
                groups.sort(key=lambda g: (("J1" in g), -len(g), max((card_by_id(self.hands[pid], x) or {"rank":99})["rank"] for x in g)))
                g = groups[0]
            return {"type":"play","cards":g}
        return {"type":"pass"} if self.last_play else None

from pathlib import Path

p = Path('index.html')
s = p.read_text(encoding='utf-8')

def rep(old: str, new: str, label: str):
    global s
    if old not in s:
        raise SystemExit(f'missing target: {label}')
    s = s.replace(old, new, 1)

rep('BOUNCE TANKS v4.1', 'BOUNCE TANKS v4.2', 'version')
rep('v4.1では滑らかさを維持しつつ、操作中の巻き戻りを抑制。', 'v4.2ではローカル操作優先の同期に変更し、巻き戻りと入力抜けをさらに抑制。', 'hero copy')
rep(
"let code=null,role=null,hostToken=null,playerId=null,playerToken=null,ws=null,state=null,retry=null,gameMounted=false,lobbySig='',input={up:false,down:false,left:false,right:false,shoot:false},motionReleaseAt=0;",
"let code=null,role=null,hostToken=null,playerId=null,playerToken=null,ws=null,state=null,retry=null,gameMounted=false,lobbySig='',input={up:false,down:false,left:false,right:false,shoot:false},motionReleaseAt=0,lastInputHeartbeat=0,activePointers=new Map();",
'input state')

old_reconcile = '''      // The authoritative state is intentionally a little behind the local prediction.
      // While the player is holding a control, ignore that normal latency-sized gap so
      // the tank never gets tugged backwards every snapshot. Reconcile only meaningful drift.
      let err=Math.hypot(v.tx-v.x,v.ty-v.y),releaseGrace=!moving&&motionReleaseAt&&now-motionReleaseAt<180;
      if(err>170){v.x=v.tx;v.y=v.ty}
      else if(moving){
        if(err>42){let a=expAlpha(err>95?2.2:.65,dt);v.x+=(v.tx-v.x)*a;v.y+=(v.ty-v.y)*a}
      }else if(releaseGrace){
        if(err>78){let a=expAlpha(2.4,dt);v.x+=(v.tx-v.x)*a;v.y+=(v.ty-v.y)*a}
      }else{
        let a=expAlpha(9.5,dt);v.x+=(v.tx-v.x)*a;v.y+=(v.ty-v.y)*a;
      }

      let ad=Math.abs(angleDelta(v.ta,v.a));
      if(ad>1.25)v.a=v.ta;
      else if(turning){if(ad>.16)v.a+=angleDelta(v.ta,v.a)*expAlpha(.7,dt)}
      else if(releaseGrace){if(ad>.3)v.a+=angleDelta(v.ta,v.a)*expAlpha(2.2,dt)}
      else v.a+=angleDelta(v.ta,v.a)*expAlpha(10,dt);'''
new_reconcile = '''      // v4.2: while a human is actively steering, the local tank is display-authoritative.
      // Server snapshots are still stored as targets, but never pull the tank backwards
      // during a press. After release we wait for the reliable WebSocket input to reach
      // the server, then reconcile smoothly without teleporting.
      let err=Math.hypot(v.tx-v.x,v.ty-v.y),releaseGrace=!moving&&motionReleaseAt&&now-motionReleaseAt<360;
      if(!moving&&!releaseGrace){
        let rate=err>120?4.8:err>45?3.4:err>10?5.8:11.5,a=expAlpha(rate,dt);
        v.x+=(v.tx-v.x)*a;v.y+=(v.ty-v.y)*a;
      }

      if(!moving&&!releaseGrace){
        let ad=Math.abs(angleDelta(v.ta,v.a)),rate=ad>.8?4.8:ad>.2?6.5:11;
        v.a+=angleDelta(v.ta,v.a)*expAlpha(rate,dt);
      }'''
rep(old_reconcile, new_reconcile, 'reconciliation')

rep(
"function startRenderLoop(){if(rafId)return;lastFrameAt=performance.now();const loop=now=>{rafId=requestAnimationFrame(loop);let dt=Math.min(.04,(now-lastFrameAt)/1000);lastFrameAt=now;advanceVisual(dt);if(state&&gameMounted)draw(state)};rafId=requestAnimationFrame(loop)}",
"function startRenderLoop(){if(rafId)return;lastFrameAt=performance.now();const loop=now=>{rafId=requestAnimationFrame(loop);let dt=Math.min(.04,(now-lastFrameAt)/1000);lastFrameAt=now;if((motionHeld()||input.shoot)&&now-lastInputHeartbeat>120){lastInputHeartbeat=now;sendInput()}advanceVisual(dt);if(state&&gameMounted)draw(state)};rafId=requestAnimationFrame(loop)}",
'input heartbeat')

rep(
"function resetInput(){let changed=false;for(let k in input){if(input[k])changed=true;input[k]=false}$$('[data-key]').forEach(b=>b.classList.remove('on'));if(changed)sendInput()}",
"function resetInput(){let changed=false;activePointers.clear();for(let k in input){if(input[k])changed=true;input[k]=false}$$('[data-key]').forEach(b=>b.classList.remove('on'));if(changed)sendInput()}",
'reset input')

rep('⚡ v4.1 SMOOTH CONTROL', '⚡ v4.2 LOCAL CONTROL', 'badge')

old_bind = "function bindHoldButton(b){let k=b.dataset.key,on=e=>{e.preventDefault();if(b.disabled)return;try{b.setPointerCapture(e.pointerId)}catch{}b.classList.add('on');setInput(k,true);if(k==='shoot'){instantFireFeedback();send('shoot')}},off=e=>{e.preventDefault();b.classList.remove('on');setInput(k,false);try{if(b.hasPointerCapture(e.pointerId))b.releasePointerCapture(e.pointerId)}catch{}};b.addEventListener('pointerdown',on,{passive:false});b.addEventListener('pointerup',off,{passive:false});b.addEventListener('pointercancel',off,{passive:false});b.addEventListener('lostpointercapture',()=>{b.classList.remove('on');setInput(k,false)});b.addEventListener('contextmenu',e=>e.preventDefault());b.addEventListener('selectstart',e=>e.preventDefault());b.addEventListener('dragstart',e=>e.preventDefault())}"
new_bind = "function releasePointer(pointerId){let k=activePointers.get(pointerId);if(!k)return;activePointers.delete(pointerId);if(![...activePointers.values()].includes(k)){let b=$(`[data-key=\"${k}\"]`);b?.classList.remove('on');setInput(k,false)}}\nfunction bindHoldButton(b){let k=b.dataset.key,on=e=>{e.preventDefault();if(b.disabled)return;activePointers.set(e.pointerId,k);try{b.setPointerCapture(e.pointerId)}catch{}b.classList.add('on');setInput(k,true);if(k==='shoot'){instantFireFeedback();send('shoot')}};b.addEventListener('pointerdown',on,{passive:false});b.addEventListener('pointerup',e=>{e.preventDefault();releasePointer(e.pointerId)},{passive:false});b.addEventListener('pointercancel',e=>{e.preventDefault();releasePointer(e.pointerId)},{passive:false});b.addEventListener('lostpointercapture',()=>{});b.addEventListener('contextmenu',e=>e.preventDefault());b.addEventListener('selectstart',e=>e.preventDefault());b.addEventListener('dragstart',e=>e.preventDefault())}"
rep(old_bind, new_bind, 'pointer controls')

rep(
"const keyMap={ArrowUp:'up'",
"addEventListener('pointerup',e=>releasePointer(e.pointerId),true);addEventListener('pointercancel',e=>releasePointer(e.pointerId),true);\nconst keyMap={ArrowUp:'up'",
'global pointer release')

p.write_text(s, encoding='utf-8')
print('v4.2 applied')

"""
Dashboard web do Jarvis — Interface HUD completa.

Serve:
  GET  /              — Dashboard principal (HUD estilo Iron Man)
  GET  /mobile        — Interface mobile com WebRTC para falar pelo celular
  GET  /api/status    — JSON com estado atual
  GET  /api/history   — JSON com histórico de conversas
  POST /api/command   — Envia comando de texto ao Jarvis
  WS   /ws            — WebSocket para atualizações em tempo real
"""

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from logging_setup import get_logger

logger = get_logger(__name__)

_AUDIT_LOG = "audit_log.jsonl"
_MAX_CONVERSATIONS = 100


class DashboardServer:
    def __init__(self, cfg) -> None:
        self._port: int = cfg.get("dashboard", "port", default=7432)
        self._enabled: bool = cfg.get("dashboard", "enabled", default=True)
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Estado compartilhado (thread-safe via lock)
        self._lock = threading.Lock()
        self._state: dict = {
            "status": "idle",
            "last_input": "",
            "last_response": "",
            "model": "—",
            "emotion": "calm",
            "interactions_today": 0,
            "uptime_start": time.time(),
            "memory_count": 0,
        }
        self._conversations: list = []
        self._text_callback = None  # injetado por main.py para enviar comandos

    def set_text_callback(self, cb) -> None:
        """Permite que o dashboard envie texto ao pipeline principal."""
        self._text_callback = cb

    def update_status(self, status: str) -> None:
        with self._lock:
            self._state["status"] = status

    def update_model(self, model: str) -> None:
        with self._lock:
            self._state["model"] = model

    def update_emotion(self, emotion: str) -> None:
        with self._lock:
            self._state["emotion"] = emotion

    def add_conversation(self, role: str, text: str, model: str = "") -> None:
        with self._lock:
            self._conversations.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "text": text,
                "model": model,
            })
            if len(self._conversations) > _MAX_CONVERSATIONS:
                self._conversations = self._conversations[-_MAX_CONVERSATIONS:]
            if role == "user":
                self._state["last_input"] = text
                self._state["interactions_today"] += 1
            elif role == "jarvis":
                self._state["last_response"] = text
                self._state["model"] = model or self._state["model"]

    def get_state(self) -> dict:
        with self._lock:
            uptime_s = int(time.time() - self._state["uptime_start"])
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)
            return {**self._state, "uptime": f"{h:02d}:{m:02d}:{s:02d}"}

    def get_conversations(self, limit: int = 50) -> list:
        with self._lock:
            return self._conversations[-limit:]

    def start(self) -> None:
        if not self._enabled:
            return
        handler = self._make_handler()
        try:
            self._server = HTTPServer(("0.0.0.0", self._port), handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True, name="dashboard"
            )
            self._thread.start()
            logger.info("Dashboard: http://localhost:%d", self._port)
        except OSError as e:
            logger.warning("Dashboard não pôde iniciar na porta %d: %s", self._port, e)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def _make_handler(self):
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass  # silencia logs HTTP

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    self._serve_html(dashboard._render_viz())
                elif path == "/dashboard":
                    self._serve_html(dashboard._render_dashboard())
                elif path == "/mobile":
                    self._serve_html(dashboard._render_mobile())
                elif path == "/api/status":
                    self._serve_json(dashboard.get_state())
                elif path == "/api/history":
                    self._serve_json({"conversations": dashboard.get_conversations()})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                path = urlparse(self.path).path
                if path == "/api/command":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length)
                    try:
                        data = json.loads(body)
                        text = data.get("text", "").strip()
                        if text and dashboard._text_callback:
                            threading.Thread(
                                target=dashboard._text_callback,
                                args=(text,), daemon=True
                            ).start()
                            self._serve_json({"ok": True})
                        else:
                            self._serve_json({"ok": False, "error": "empty command"})
                    except Exception as e:
                        self._serve_json({"ok": False, "error": str(e)})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def _serve_html(self, html: str):
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            def _serve_json(self, obj: dict):
                data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

        return Handler

    # ── HTML Templates ──────────────────────────────────────────────────────────

    def _render_viz(self) -> str:
        """Visualização neural de partículas — interface principal do Jarvis."""
        return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@300;600&display=swap">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { width:100%; height:100%; overflow:hidden; background:#030810; }
  canvas { display:block; position:fixed; inset:0; }
  #ui { position:fixed; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; pointer-events:none; }
  #sphere-space { flex:1; }
  #status-area { display:flex; flex-direction:column; align-items:center; gap:10px; padding-bottom:60px; }
  #status-text { font-family:'Chakra Petch',sans-serif; font-size:clamp(12px,1.4vw,18px); font-weight:600; letter-spacing:.32em; text-transform:uppercase; color:#00d4a0; text-shadow:0 0 16px rgba(0,212,160,.7),0 0 40px rgba(0,212,160,.3); transition:color .4s,text-shadow .4s; min-height:1.4em; }
  #status-text.recording { color:#ff5555; text-shadow:0 0 16px rgba(255,50,50,.7),0 0 40px rgba(255,50,50,.3); }
  #status-text.processing { color:#ffaa00; text-shadow:0 0 16px rgba(255,170,0,.7),0 0 40px rgba(255,170,0,.3); }
  #wake-hint { font-family:'Chakra Petch',sans-serif; font-size:clamp(10px,.9vw,13px); font-weight:300; letter-spacing:.18em; color:rgba(138,184,204,.3); }
  #wake-hint b { color:rgba(0,212,160,.6); font-weight:600; }
  .corner-hud { position:fixed; font-family:'Chakra Petch',sans-serif; font-size:10px; letter-spacing:.16em; color:rgba(0,212,160,.25); pointer-events:none; }
  #hud-tl { top:20px; left:24px; } #hud-tr { top:20px; right:24px; text-align:right; } #hud-bl { bottom:20px; left:24px; }
  .hud-line { line-height:1.8; } .hud-val { color:rgba(0,212,160,.5); }
  .bracket { position:fixed; width:20px; height:20px; opacity:.25; pointer-events:none; }
  .b-tl { top:10px; left:10px; border-top:1px solid #00d4a0; border-left:1px solid #00d4a0; }
  .b-tr { top:10px; right:10px; border-top:1px solid #00d4a0; border-right:1px solid #00d4a0; }
  .b-bl { bottom:10px; left:10px; border-bottom:1px solid #00d4a0; border-left:1px solid #00d4a0; }
  .b-br { bottom:10px; right:10px; border-bottom:1px solid #00d4a0; border-right:1px solid #00d4a0; }
  #nav { position:fixed; bottom:20px; right:24px; display:flex; gap:8px; pointer-events:all; }
  .nav-btn { background:transparent; border:1px solid rgba(0,212,160,.3); color:rgba(0,212,160,.5); padding:6px 14px; font-family:'Chakra Petch',sans-serif; font-size:10px; letter-spacing:.14em; cursor:pointer; border-radius:2px; transition:all .2s; }
  .nav-btn:hover { border-color:rgba(0,212,160,.7); color:#00d4a0; background:rgba(0,212,160,.06); }
  #mic-btn { width:36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:16px; }
  #mic-btn.muted { border-color:rgba(255,80,80,.4); color:rgba(255,80,80,.5); }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="ui">
  <div id="sphere-space"></div>
  <div id="status-area">
    <div id="status-text">AGUARDANDO</div>
    <div id="wake-hint">Pressione <b>CTRL_R</b> e fale &nbsp;·&nbsp; Segure <b>ESPAÇO</b> para testar</div>
  </div>
</div>
<div class="corner-hud" id="hud-tl">
  <div class="hud-line">J.A.R.V.I.S <span class="hud-val">v3.0</span></div>
  <div class="hud-line">VICTOR GERMANO</div>
</div>
<div class="corner-hud" id="hud-tr">
  <div class="hud-line">MODELO <span class="hud-val" id="h-model">—</span></div>
  <div class="hud-line">UPTIME <span class="hud-val" id="h-uptime">—</span></div>
</div>
<div class="corner-hud" id="hud-bl">
  <div class="hud-line">INTERAÇÕES <span class="hud-val" id="h-int">0</span></div>
</div>
<div class="bracket b-tl"></div><div class="bracket b-tr"></div>
<div class="bracket b-bl"></div><div class="bracket b-br"></div>
<div id="nav">
  <button class="nav-btn" id="mic-btn" title="Microfone">🎙</button>
  <button class="nav-btn" onclick="location.href='/dashboard'">HUD</button>
  <button class="nav-btn" onclick="location.href='/mobile'">MOBILE</button>
</div>
<script>
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d');
let W,H,CX,CY,R;
function resize(){W=canvas.width=innerWidth;H=canvas.height=innerHeight;CX=W/2;CY=H/2;R=Math.min(W,H)*.32;}
resize();window.addEventListener('resize',resize);
const N=680,PHI=Math.PI*(3-Math.sqrt(5)),particles=[],connections=[];
for(let i=0;i<N;i++){const y=1-(i/(N-1))*2,r=Math.sqrt(Math.max(0,1-y*y)),th=PHI*i,sc=0.92+Math.random()*.16;particles.push({bx:Math.cos(th)*r*sc,by:y*sc,bz:Math.sin(th)*r*sc,x:0,y:0,z:0,phase:Math.random()*Math.PI*2,size:.8+Math.random()*1.6,speed:.7+Math.random()*.6});}
const CD=0.36;for(let i=0;i<N;i++){const pi=particles[i];for(let j=i+1;j<N;j++){const pj=particles[j],dx=pi.bx-pj.bx,dy=pi.by-pj.by,dz=pi.bz-pj.bz,d2=dx*dx+dy*dy+dz*dz;if(d2<CD*CD)connections.push({i,j,d:Math.sqrt(d2)});}}
let analyser=null,dataArr=null,micActive=false,micMuted=false;
async function initMic(){try{const s=await navigator.mediaDevices.getUserMedia({audio:true,video:false}),ac=new AudioContext(),src=ac.createMediaStreamSource(s);analyser=ac.createAnalyser();analyser.fftSize=256;analyser.smoothingTimeConstant=.75;src.connect(analyser);dataArr=new Uint8Array(analyser.frequencyBinCount);micActive=true;}catch(e){}}
function getAudioLevel(){if(!analyser||!micActive||micMuted)return 0;analyser.getByteFrequencyData(dataArr);let s=0;for(let i=0;i<dataArr.length;i++)s+=dataArr[i];return s/(dataArr.length*255);}
document.getElementById('mic-btn').addEventListener('click',()=>{if(!micActive)initMic();else{micMuted=!micMuted;const b=document.getElementById('mic-btn');b.classList.toggle('muted',micMuted);b.innerHTML=micMuted?'🔇':'🎙';}});
initMic();
let status='idle',rotY=0,rotX=0,t=0,simPhase=0,smoothAudio=0,prevTime=performance.now();
function rotY3(x,z,a){const c=Math.cos(a),s=Math.sin(a);return[x*c+z*s,-x*s+z*c];}
function rotX3(y,z,a){const c=Math.cos(a),s=Math.sin(a);return[y*c-z*s,y*s+z*c];}
const FOV=3.2;
function proj(x,y,z){const iz=1/(z+FOV);return{sx:CX+x*iz*R*FOV,sy:CY+y*iz*R*FOV,scale:iz*FOV,depth:(z+1)/2};}
function frame(now){
  requestAnimationFrame(frame);
  const dt=Math.min(now-prevTime,32)/16.67;prevTime=now;t+=dt;
  const raw=getAudioLevel();simPhase+=.018*dt;
  const sim=.08+.06*Math.sin(simPhase);
  const target=micActive&&!micMuted?raw:sim;
  smoothAudio+=(target-smoothAudio)*.12;const al=smoothAudio;
  const rs=status==='processing'?.006:.002;rotY+=rs*dt*(1+al*2);rotX=Math.sin(t*.008)*.18;
  ctx.fillStyle='rgba(3,8,16,.18)';ctx.fillRect(0,0,W,H);
  const projected=particles.map((p)=>{
    let{bx,by,bz,phase,speed}=p;
    const disp=status==='recording'?1+al*1.2*(.6+.4*Math.sin(phase+t*.05*speed)):status==='processing'?1+.08*Math.sin(t*.04*speed+phase):1+al*.3*Math.sin(phase+t*.02);
    let x=bx*disp,y=by*disp,z=bz*disp;
    if(status==='recording'&&al>.1){const tu=al*.15;x+=Math.sin(phase*3.7+t*.03*speed)*tu;y+=Math.cos(phase*2.3+t*.025*speed)*tu;z+=Math.sin(phase*1.9+t*.035*speed)*tu;}
    const[rx,rz1]=rotY3(x,z,rotY);const[ry,rz2]=rotX3(y,rz1,rotX);return proj(rx,ry,rz2);
  });
  const bc=status==='recording'?[255,100,100]:status==='processing'?[255,180,60]:[160,220,255];
  const gc=status==='recording'?[255,80,80]:status==='processing'?[255,160,40]:[80,180,255];
  for(const{i,j,d}of connections){const pi=projected[i],pj=projected[j];if(particles[i].z<-.7&&particles[j].z<-.7)continue;const ba=(1-d/CD)*.18,ab=al*.4,df=(pi.depth+pj.depth)*.5,alpha=(ba+ab*ba*3)*(.2+df*.8);if(alpha<.005)continue;ctx.beginPath();ctx.moveTo(pi.sx,pi.sy);ctx.lineTo(pj.sx,pj.sy);ctx.strokeStyle=`rgba(${gc[0]},${gc[1]},${gc[2]},${Math.min(alpha,.6)})`;ctx.lineWidth=.4+df*.4;ctx.stroke();}
  const order=projected.map((p,i)=>({p,i})).sort((a,b)=>a.p.depth-b.p.depth);
  for(const{p,i}of order){const part=particles[i],da=.25+p.depth*.75,ag=al*1.2,sz=part.size*p.scale*R*(1+al*.6);if(sz<.2)continue;if(p.depth>.4){const grd=ctx.createRadialGradient(p.sx,p.sy,0,p.sx,p.sy,sz*3.5);grd.addColorStop(0,`rgba(${gc[0]},${gc[1]},${gc[2]},${da*.12*(1+ag)})`);grd.addColorStop(1,'rgba(0,0,0,0)');ctx.beginPath();ctx.arc(p.sx,p.sy,sz*3.5,0,Math.PI*2);ctx.fillStyle=grd;ctx.fill();}ctx.beginPath();ctx.arc(p.sx,p.sy,Math.max(sz,.3),0,Math.PI*2);ctx.fillStyle=`rgba(${Math.min(255,bc[0]+ag*40)},${bc[1]},${bc[2]},${da})`;ctx.fill();}
  if(al>.35&&status==='recording'){ctx.save();ctx.globalCompositeOperation='screen';ctx.globalAlpha=(al-.35)*.4;ctx.fillStyle='rgba(0,120,255,0.15)';ctx.fillRect(0,0,W,H);ctx.restore();}
}
requestAnimationFrame(frame);
const STATUS_LABEL={idle:'AGUARDANDO',recording:'OUVINDO...',processing:'PROCESSANDO...',error:'ERRO'};
async function poll(){try{const r=await fetch('/api/status',{signal:AbortSignal.timeout(1200)}),d=await r.json();setStatus(d.status||'idle');const m=document.getElementById('h-model');if(m)m.textContent=d.model||'—';const u=document.getElementById('h-uptime');if(u)u.textContent=d.uptime||'—';const it=document.getElementById('h-int');if(it)it.textContent=d.interactions_today??'—';}catch{}}
function setStatus(s){status=s;const el=document.getElementById('status-text');el.textContent=STATUS_LABEL[s]||s.toUpperCase();el.className=s;}
poll();setInterval(poll,600);
let dr=false;
window.addEventListener('keydown',e=>{if(e.code==='Space'&&!e.target.matches('input,textarea')){e.preventDefault();if(!dr){dr=true;setStatus('recording');}}});
window.addEventListener('keyup',e=>{if(e.code==='Space'){dr=false;setStatus('idle');}});
</script>
</body>
</html>"""

    def _render_dashboard(self) -> str:
        state = self.get_state()
        convs = self.get_conversations(30)
        status = state["status"]
        status_color = {"idle": "#00ffcc", "recording": "#ff3333", "processing": "#ffaa00", "error": "#ff6600"}.get(status, "#00ffcc")
        conv_html = ""
        for c in reversed(convs):
            ts = c["ts"][11:19]
            role_label = "Victor" if c["role"] == "user" else "Jarvis"
            role_color = "#00aaff" if c["role"] == "user" else "#00ffcc"
            model_tag = f'<span style="color:#444;font-size:0.65rem;margin-left:8px">{c["model"]}</span>' if c.get("model") else ""
            text = c["text"].replace("<", "&lt;").replace(">", "&gt;")
            conv_html += f"""
            <div class="msg msg-{c['role']}">
              <span class="msg-meta" style="color:{role_color}">{role_label}</span>
              {model_tag}
              <span class="msg-time">{ts}</span>
              <div class="msg-text">{text}</div>
            </div>"""

        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S — HUD</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --cyan: #00ffcc; --blue: #00aaff; --red: #ff3333;
    --amber: #ffaa00; --bg: #020c14; --surface: #050f1a;
    --border: rgba(0,255,204,0.15); --text: #8ab8cc;
  }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Share Tech Mono', monospace;
         min-height: 100vh; overflow-x: hidden; }}

  /* Scanline overlay */
  body::before {{ content:''; position:fixed; inset:0; pointer-events:none; z-index:9999;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px); }}

  /* Header */
  .hud-header {{ display:flex; align-items:center; justify-content:space-between;
    padding: 16px 32px; border-bottom: 1px solid var(--border);
    background: linear-gradient(90deg, var(--surface), transparent); }}
  .logo {{ font-family: 'Orbitron', sans-serif; font-size: 1.4rem; font-weight:900;
           color: var(--cyan); letter-spacing: 8px; text-shadow: 0 0 20px var(--cyan); }}
  .logo span {{ color: #004433; }}
  .header-right {{ display:flex; align-items:center; gap: 24px; font-size:0.7rem; letter-spacing:2px; }}
  .uptime {{ color: #445; }}
  .status-pill {{ padding: 5px 14px; border-radius: 2px; font-size: 0.7rem; letter-spacing: 3px;
    border: 1px solid {status_color}; color: {status_color};
    {"animation: pulse 1s infinite;" if status == "recording" else ""}
    text-shadow: 0 0 8px {status_color}; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}

  /* Main grid */
  .hud-main {{ display: grid; grid-template-columns: 280px 1fr 260px; gap: 0; height: calc(100vh - 65px); }}

  /* Left panel */
  .panel-left {{ border-right: 1px solid var(--border); padding: 24px 20px; overflow-y: auto; }}
  .stat-block {{ margin-bottom: 24px; }}
  .stat-label {{ color: rgba(0,255,204,0.4); font-size: 0.65rem; letter-spacing: 3px;
    text-transform: uppercase; margin-bottom: 6px; }}
  .stat-value {{ color: var(--cyan); font-size: 1.1rem; }}
  .stat-value.big {{ font-size: 2.4rem; font-family: 'Orbitron', sans-serif; font-weight:700;
    text-shadow: 0 0 15px var(--cyan); }}
  .divider {{ border: none; border-top: 1px solid var(--border); margin: 20px 0; }}
  .section-title {{ color: rgba(0,255,204,0.5); font-size: 0.6rem; letter-spacing: 4px;
    text-transform: uppercase; margin-bottom: 12px; }}
  .model-badge {{ display:inline-block; padding:3px 10px; border:1px solid rgba(0,170,255,0.3);
    color: var(--blue); font-size:0.7rem; border-radius:2px; }}
  .emotion-badge {{ display:inline-block; padding:3px 10px; border:1px solid rgba(0,255,204,0.2);
    color: var(--cyan); font-size:0.7rem; border-radius:2px; }}

  /* Ring chart */
  .ring-container {{ display:flex; justify-content:center; margin: 20px 0; }}
  .ring {{ position:relative; width:120px; height:120px; }}
  .ring svg {{ transform: rotate(-90deg); }}
  .ring-label {{ position:absolute; inset:0; display:flex; flex-direction:column;
    align-items:center; justify-content:center; }}
  .ring-num {{ font-family:'Orbitron',sans-serif; font-size:1.6rem; color:var(--cyan); font-weight:700; }}
  .ring-sub {{ font-size:0.55rem; color:rgba(0,255,204,0.5); letter-spacing:2px; }}

  /* Center — conversation */
  .panel-center {{ display:flex; flex-direction:column; overflow:hidden; }}
  .conv-header {{ padding: 16px 24px; border-bottom: 1px solid var(--border);
    display:flex; align-items:center; justify-content:space-between; }}
  .conv-title {{ color: var(--cyan); font-size: 0.7rem; letter-spacing:4px; }}
  .conv-body {{ flex:1; overflow-y:auto; padding: 20px 24px; display:flex; flex-direction:column; gap:16px; }}
  .msg {{ padding: 12px 16px; border-radius: 3px; position:relative; }}
  .msg-user {{ background: rgba(0,170,255,0.05); border-left: 2px solid var(--blue); }}
  .msg-jarvis {{ background: rgba(0,255,204,0.04); border-left: 2px solid var(--cyan); }}
  .msg-meta {{ font-size:0.7rem; letter-spacing:2px; font-weight:bold; }}
  .msg-time {{ font-size:0.65rem; color:#334; margin-left:8px; }}
  .msg-text {{ color:#9ab; font-size:0.85rem; line-height:1.6; margin-top:6px; }}
  .conv-input {{ border-top: 1px solid var(--border); padding: 16px 24px; display:flex; gap:12px; }}
  .conv-input input {{ flex:1; background: var(--surface); border: 1px solid var(--border);
    color: var(--cyan); padding: 10px 14px; font-family: 'Share Tech Mono', monospace;
    font-size:0.85rem; outline:none; border-radius:2px; }}
  .conv-input input:focus {{ border-color: var(--cyan); box-shadow: 0 0 8px rgba(0,255,204,0.1); }}
  .conv-input button {{ background: transparent; border: 1px solid var(--cyan); color: var(--cyan);
    padding: 10px 20px; cursor:pointer; font-family: 'Share Tech Mono', monospace;
    font-size:0.8rem; letter-spacing:2px; transition: all 0.2s; border-radius:2px; }}
  .conv-input button:hover {{ background: rgba(0,255,204,0.1); box-shadow: 0 0 12px rgba(0,255,204,0.2); }}

  /* Right panel */
  .panel-right {{ border-left: 1px solid var(--border); padding: 24px 20px; overflow-y: auto; }}
  .link-btn {{ display:block; padding:10px 14px; margin-bottom:8px; border:1px solid var(--border);
    color:var(--text); text-decoration:none; font-size:0.75rem; letter-spacing:1px;
    transition: all 0.2s; border-radius:2px; }}
  .link-btn:hover {{ border-color:var(--cyan); color:var(--cyan); background:rgba(0,255,204,0.05); }}
  .link-btn .icon {{ color:var(--cyan); margin-right:8px; }}

  /* Corner brackets decoration */
  .corner {{ position:fixed; width:40px; height:40px; opacity:0.3; }}
  .corner-tl {{ top:8px; left:8px; border-top:1px solid var(--cyan); border-left:1px solid var(--cyan); }}
  .corner-tr {{ top:8px; right:8px; border-top:1px solid var(--cyan); border-right:1px solid var(--cyan); }}
  .corner-bl {{ bottom:8px; left:8px; border-bottom:1px solid var(--cyan); border-left:1px solid var(--cyan); }}
  .corner-br {{ bottom:8px; right:8px; border-bottom:1px solid var(--cyan); border-right:1px solid var(--cyan); }}

  ::-webkit-scrollbar {{ width:4px; }} ::-webkit-scrollbar-track {{ background:transparent; }}
  ::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:2px; }}
</style>
</head>
<body>
<div class="corner corner-tl"></div>
<div class="corner corner-tr"></div>
<div class="corner corner-bl"></div>
<div class="corner corner-br"></div>

<header class="hud-header">
  <div class="logo">J<span>.</span>A<span>.</span>R<span>.</span>V<span>.</span>I<span>.</span>S</div>
  <div class="header-right">
    <span class="uptime">UPTIME {state["uptime"]}</span>
    <span class="uptime">VICTOR GERMANO</span>
    <span class="status-pill">{status.upper()}</span>
  </div>
</header>

<main class="hud-main">
  <!-- LEFT PANEL -->
  <aside class="panel-left">
    <div class="section-title">Sistema</div>

    <div class="ring-container">
      <div class="ring">
        <svg viewBox="0 0 120 120" width="120" height="120">
          <circle cx="60" cy="60" r="50" fill="none" stroke="rgba(0,255,204,0.08)" stroke-width="8"/>
          <circle cx="60" cy="60" r="50" fill="none" stroke="#00ffcc" stroke-width="8"
            stroke-dasharray="{min(state["interactions_today"]*10, 314)} 314"
            stroke-linecap="round" style="filter:drop-shadow(0 0 4px #00ffcc)"/>
        </svg>
        <div class="ring-label">
          <span class="ring-num">{state["interactions_today"]}</span>
          <span class="ring-sub">HOJE</span>
        </div>
      </div>
    </div>

    <hr class="divider">
    <div class="stat-block">
      <div class="stat-label">Modelo Ativo</div>
      <span class="model-badge">{state["model"]}</span>
    </div>
    <div class="stat-block">
      <div class="stat-label">Estado Emocional</div>
      <span class="emotion-badge">{state["emotion"].upper()}</span>
    </div>
    <div class="stat-block">
      <div class="stat-label">Última Entrada</div>
      <div class="stat-value" style="font-size:0.8rem;color:#668;word-break:break-word">
        {(state["last_input"][:80] + "…") if len(state["last_input"]) > 80 else state["last_input"] or "—"}
      </div>
    </div>
    <hr class="divider">
    <div class="section-title">Memória</div>
    <div class="stat-block">
      <div class="stat-label">Registros</div>
      <div class="stat-value big">{state["memory_count"]}</div>
    </div>
  </aside>

  <!-- CENTER PANEL -->
  <section class="panel-center">
    <div class="conv-header">
      <span class="conv-title">REGISTRO DE CONVERSAS</span>
      <span style="color:#334;font-size:0.65rem">ATUALIZA A CADA 3s</span>
    </div>
    <div class="conv-body" id="conv">
      {conv_html if conv_html else '<div style="color:#334;text-align:center;margin-top:40px;font-size:0.8rem">Nenhuma conversa ainda. Pressione Ctrl_R e fale.</div>'}
    </div>
    <div class="conv-input">
      <input type="text" id="cmd" placeholder="Digite um comando para o Jarvis..." onkeydown="if(event.key==='Enter')send()">
      <button onclick="send()">ENVIAR</button>
    </div>
  </section>

  <!-- RIGHT PANEL -->
  <aside class="panel-right">
    <div class="section-title">Acesso</div>
    <a class="link-btn" href="/mobile" target="_blank"><span class="icon">📱</span>Interface Mobile</a>
    <a class="link-btn" href="/api/status" target="_blank"><span class="icon">⚡</span>API Status</a>
    <a class="link-btn" href="/api/history" target="_blank"><span class="icon">📋</span>API Histórico</a>
    <hr class="divider">
    <div class="section-title">Integrações</div>
    <div class="link-btn"><span class="icon" style="color:#1db954">♪</span>Spotify</div>
    <div class="link-btn"><span class="icon" style="color:#fff">⊛</span>GitHub</div>
    <div class="link-btn"><span class="icon" style="color:#4285f4">◉</span>Google</div>
    <div class="link-btn"><span class="icon">⌨</span>VS Code</div>
    <hr class="divider">
    <div class="section-title">Última Resposta</div>
    <div style="color:#668;font-size:0.75rem;line-height:1.7;word-break:break-word">
      {(state["last_response"][:200] + "…") if len(state["last_response"]) > 200 else state["last_response"] or "—"}
    </div>
  </aside>
</main>

<script>
  // Auto-refresh
  setTimeout(() => location.reload(), 3000);

  // Scroll conversation to bottom
  const conv = document.getElementById('conv');
  if (conv) conv.scrollTop = conv.scrollHeight;

  // Send command
  async function send() {{
    const inp = document.getElementById('cmd');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    await fetch('/api/command', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{text}})
    }});
  }}
</script>
</body>
</html>"""

    def _render_mobile(self) -> str:
        return """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Jarvis Mobile</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Share+Tech+Mono&display=swap');
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#020c14; color:#8ab8cc; font-family:'Share Tech Mono',monospace;
         min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:20px; }
  .logo { font-family:'Orbitron',sans-serif; font-size:1.8rem; font-weight:700;
          color:#00ffcc; letter-spacing:6px; text-shadow:0 0 20px #00ffcc; margin-bottom:8px; }
  .sub { color:#004433; font-size:0.7rem; letter-spacing:3px; margin-bottom:48px; }
  .ring-btn { width:180px; height:180px; border-radius:50%; border:2px solid #00ffcc;
    background:transparent; cursor:pointer; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:8px; margin:0 auto 32px;
    transition:all 0.2s; box-shadow:0 0 30px rgba(0,255,204,0.1); position:relative; }
  .ring-btn::before { content:''; position:absolute; inset:-8px; border-radius:50%;
    border:1px solid rgba(0,255,204,0.2); }
  .ring-btn::after { content:''; position:absolute; inset:-16px; border-radius:50%;
    border:1px solid rgba(0,255,204,0.08); }
  .ring-btn.recording { border-color:#ff3333; box-shadow:0 0 40px rgba(255,50,50,0.3);
    background:rgba(255,50,50,0.05); animation:pulse 1s infinite; }
  .ring-btn.recording::before { border-color:rgba(255,50,50,0.3); }
  @keyframes pulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.03)} }
  .mic-icon { font-size:3rem; }
  .btn-label { font-size:0.7rem; letter-spacing:3px; color:#00ffcc; }
  .status { text-align:center; color:#00ffcc; font-size:0.8rem; letter-spacing:2px;
    min-height:24px; margin-bottom:24px; }
  .transcript { background:rgba(0,255,204,0.04); border:1px solid rgba(0,255,204,0.1);
    border-radius:4px; padding:16px; width:100%; max-width:400px; min-height:80px;
    font-size:0.85rem; color:#9ab; line-height:1.6; margin-bottom:16px; }
  .response { background:rgba(0,170,255,0.04); border:1px solid rgba(0,170,255,0.1);
    border-radius:4px; padding:16px; width:100%; max-width:400px; min-height:60px;
    font-size:0.85rem; color:#00ffcc; line-height:1.6; }
  .resp-label { font-size:0.6rem; letter-spacing:3px; color:rgba(0,255,204,0.4); margin-bottom:8px; }
  .send-btn { background:transparent; border:1px solid #00ffcc; color:#00ffcc;
    padding:12px 32px; font-family:'Share Tech Mono',monospace; font-size:0.8rem;
    letter-spacing:3px; cursor:pointer; border-radius:2px; margin-top:8px;
    width:100%; max-width:400px; }
  .text-input { background:rgba(0,255,204,0.03); border:1px solid rgba(0,255,204,0.2);
    color:#00ffcc; padding:12px 14px; font-family:'Share Tech Mono',monospace;
    font-size:0.85rem; border-radius:2px; width:100%; max-width:400px; margin-bottom:8px;
    outline:none; }
</style>
</head>
<body>
<div class="logo">JARVIS</div>
<div class="sub">ASSISTENTE PESSOAL — MOBILE</div>

<button class="ring-btn" id="micBtn" onclick="toggleRecording()">
  <span class="mic-icon">🎙</span>
  <span class="btn-label" id="btnLabel">SEGURAR</span>
</button>

<div class="status" id="status">Pronto. Toque para falar.</div>

<div class="transcript" id="transcript">
  <div class="resp-label">VOCÊ DISSE</div>
  <span id="transcriptText">—</span>
</div>

<div class="response" id="response">
  <div class="resp-label">JARVIS</div>
  <span id="responseText">Aguardando...</span>
</div>

<br>
<div style="color:#334;font-size:0.65rem;letter-spacing:2px;text-align:center;margin:8px 0">— OU DIGITE —</div>
<input class="text-input" type="text" id="textInput" placeholder="Digite seu comando..."
  onkeydown="if(event.key==='Enter')sendText()">
<button class="send-btn" onclick="sendText()">ENVIAR COMANDO</button>

<script>
let mediaRecorder = null;
let audioChunks = [];
let recording = false;
const HOST = window.location.origin;

async function toggleRecording() {
  if (!recording) await startRec();
  else await stopRec();
}

async function startRec() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
    mediaRecorder.start();
    recording = true;
    document.getElementById('micBtn').classList.add('recording');
    document.getElementById('btnLabel').textContent = 'GRAVANDO';
    document.getElementById('status').textContent = '● Gravando... toque para parar';
  } catch(e) {
    document.getElementById('status').textContent = 'Erro: ' + e.message;
  }
}

async function stopRec() {
  if (!mediaRecorder) return;
  mediaRecorder.stop();
  mediaRecorder.stream.getTracks().forEach(t => t.stop());
  recording = false;
  document.getElementById('micBtn').classList.remove('recording');
  document.getElementById('btnLabel').textContent = 'SEGURAR';
  document.getElementById('status').textContent = '⏳ Processando...';

  mediaRecorder.onstop = async () => {
    const blob = new Blob(audioChunks, {type:'audio/webm'});
    const form = new FormData();
    form.append('audio', blob, 'audio.webm');
    try {
      const r = await fetch(HOST + '/api/voice', {method:'POST', body: form});
      const data = await r.json();
      document.getElementById('transcriptText').textContent = data.transcript || '—';
      document.getElementById('responseText').textContent = data.response || '—';
      document.getElementById('status').textContent = 'Pronto.';
    } catch(e) {
      document.getElementById('status').textContent = 'Erro de conexão';
    }
  };
}

async function sendText() {
  const text = document.getElementById('textInput').value.trim();
  if (!text) return;
  document.getElementById('textInput').value = '';
  document.getElementById('status').textContent = '⏳ Enviando...';
  document.getElementById('transcriptText').textContent = text;
  try {
    const r = await fetch(HOST + '/api/command', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text})
    });
    document.getElementById('status').textContent = 'Comando enviado. Aguardando resposta...';
    // Poll for response
    setTimeout(async () => {
      const h = await fetch(HOST + '/api/history');
      const d = await h.json();
      const last = d.conversations.filter(c => c.role === 'jarvis').slice(-1)[0];
      if (last) document.getElementById('responseText').textContent = last.text;
      document.getElementById('status').textContent = 'Pronto.';
    }, 3000);
  } catch(e) {
    document.getElementById('status').textContent = 'Erro: ' + e.message;
  }
}
</script>
</body>
</html>"""

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

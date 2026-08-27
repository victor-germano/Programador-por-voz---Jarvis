#!/usr/bin/env python3
"""
J.A.R.V.I.S — Assistente Pessoal de Voz de Victor Germano

Pipeline completo:
  [Microfone] → AudioCapture → EmotionDetector → Transcriber
      → CommandInterpreter
          ├── Controle local (cancela, interrompe, repete)
          ├── Integrações (Spotify, GitHub, Google, VS Code)
          └── Ditado → ModelRouter → Claude (Haiku/Sonnet/Opus)
                           → Humanizer → TTS → AudioPlayer
                           → PersistentMemory → AuditLogger

Módulos ativos:
  - Memória persistente entre sessões (SQLite)
  - Roteamento automático de modelos por complexidade
  - Contexto do VS Code (arquivo aberto)
  - Spotify, GitHub, Google Calendar/Gmail por voz
  - Monitor proativo de erros no terminal
  - Dashboard web em localhost:7432
  - Detecção de emoção no tom de voz
"""

import argparse
import os
import queue
import signal
import sys
import threading
import time
from typing import Optional

import numpy as np

from config import load_config
from logging_setup import configure_logging, get_logger
from audio_capture import AudioCapture, list_microphones
from audio_capture.devices import print_microphones
from transcription import Transcriber
from command_interpreter import CommandInterpreter
from command_interpreter.control_commands import ControlCommand, CommandType
from text_injector import TextInjector
from history import HistoryManager
from tray import TrayApp
from tray.overlay import RecordingOverlay
from audit import AuditLogger
from response_capture import ClaudeCapture
from response_capture.anthropic_client import AnthropicClient
from response_humanizer import Humanizer
from tts import create_tts_engine
from tts.player import AudioPlayer
from tts.streaming_pipeline import StreamingTTSPipeline
from mcp_servers import MCPManager


class _PipelineStarting:
    """Sentinel placed in _streaming_pipeline while the pipeline thread is launching
    but before the StreamingTTSPipeline object exists.  Exposes .stop() so all
    callers can call it unconditionally without hasattr checks."""

    def stop(self) -> None:
        pass  # generation counter handles cancellation at this stage

# ── Novos módulos ───────────────────────────────────────────────────────────────
from memory.persistent import PersistentMemory
from model_router.router import ModelRouter
from emotion.detector import EmotionDetector
from proactive.monitor import ProactiveMonitor
from dashboard.app import DashboardServer
from integrations.spotify_integration import SpotifyIntegration
from integrations.github_integration import GitHubIntegration
from integrations.google_integration import GoogleIntegration
from integrations.vscode_context import VSCodeContext
from remote.tunnel import RemoteTunnel

logger = get_logger(__name__)


class VoiceProgrammer:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self._cfg = load_config(config_path)
        self._setup_logging()

        logger.info("Inicializando J.A.R.V.I.S...")

        # ── Módulos base ────────────────────────────────────────────────────────
        self._history = HistoryManager(self._cfg)
        self._audit = AuditLogger(self._cfg)
        self._injector = TextInjector(self._cfg)
        self._interpreter = CommandInterpreter(self._cfg)
        self._tray = TrayApp(self._cfg, on_quit=self._shutdown)
        self._overlay = RecordingOverlay()

        # ── Memória persistente ─────────────────────────────────────────────────
        if self._cfg.get("memory", "enabled", default=True):
            db = self._cfg.get("memory", "db_file", default="jarvis_memory.db")
            self._memory = PersistentMemory(db)
            logger.info("Memória persistente ativa: %s", db)
        else:
            self._memory = None

        # ── Roteamento de modelos ───────────────────────────────────────────────
        self._model_router = ModelRouter(self._cfg) if self._cfg.get(
            "model_routing", "enabled", default=True
        ) else None

        # ── Detecção de emoção ──────────────────────────────────────────────────
        self._emotion_detector = EmotionDetector()
        self._last_emotion: Optional[dict] = None

        # ── Integrações externas ────────────────────────────────────────────────
        self._spotify = SpotifyIntegration()
        self._github = GitHubIntegration()
        self._google = GoogleIntegration()
        self._vscode = VSCodeContext()

        # ── Monitor proativo ────────────────────────────────────────────────────
        log_file = (self._cfg.get("logging", "file") or "programador_por_voz.log")
        self._proactive = ProactiveMonitor(log_file=log_file)

        # ── Dashboard web ───────────────────────────────────────────────────────
        self._dashboard = DashboardServer()

        # ── TTS e player ────────────────────────────────────────────────────────
        self._tts_enabled: bool = self._cfg.get("tts", "enabled", default=True)
        self._tts_engine = None
        self._audio_player = AudioPlayer(
            sample_rate=self._cfg.get("tts", "sample_rate", default=22050)
        )
        self._humanizer = Humanizer(self._cfg)
        self._stop_speaking_phrases: list[str] = self._cfg.get(
            "tts", "stop_speaking_phrases",
            default=["para de falar", "cala a boca", "silencia", "espera", "chega"]
        )
        self._streaming_pipeline: Optional[StreamingTTSPipeline] = None
        self._pipeline_gen: int = 0  # incremented on every new request; old threads bail when they see a mismatch

        # ── Captura de resposta do Claude ───────────────────────────────────────
        self._claude_capture_enabled: bool = self._cfg.get(
            "claude_capture", "enabled", default=True
        )
        if self._claude_capture_enabled:
            if os.environ.get("ANTHROPIC_API_KEY"):
                self._claude = AnthropicClient(self._cfg)
                logger.info("Backend: API Anthropic direta")
            else:
                self._claude = ClaudeCapture(self._cfg)
                logger.info("Backend: claude --print subprocess")
        else:
            self._claude = None

        # ── MCP ─────────────────────────────────────────────────────────────────
        self._mcp = MCPManager(self._cfg)

        self._transcriber: Optional[Transcriber] = None

        # ── Captura de áudio ────────────────────────────────────────────────────
        self._capture = AudioCapture(self._cfg, on_audio=self._on_audio)

        def _on_status(status: str):
            self._tray.set_status(status)
            self._overlay.set_status(status)
            self._dashboard.update_status(status)
            # Para o TTS imediatamente ao pressionar a tecla de gravação,
            # antes de o microfone começar a capturar — evita o mic gravar a própria voz do Jarvis
            if status == "recording":
                self._pipeline_gen += 1  # invalidates any in-flight pipeline thread
                if self._streaming_pipeline is not None:
                    self._streaming_pipeline.stop()
                    self._streaming_pipeline = None
                self._audio_player.stop()

        self._capture.on_status_change = _on_status
        self._running = False

    def _update_status(self, status: str) -> None:
        self._tray.set_status(status)
        self._overlay.set_status(status)
        self._dashboard.update_status(status)

    def _setup_logging(self) -> None:
        log_cfg = self._cfg.get("logging") or {}
        configure_logging(
            level=log_cfg.get("level", "INFO"),
            log_file=log_cfg.get("file"),
            max_bytes=int(log_cfg.get("max_file_size_mb", 10)) * 1024 * 1024,
            backup_count=log_cfg.get("backup_count", 3),
            console=log_cfg.get("console", True),
        )

    # ── Loop principal ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._tray.start()
        self._overlay.start()
        self._update_status("idle")

        # Dashboard web
        dashboard_port = self._cfg.get("ui", "dashboard_port", default=7432)
        self._dashboard.start(port=dashboard_port)

        # Callback para envio de texto via dashboard/mobile
        def _text_from_dashboard(text: str):
            if self._transcriber is None:
                return
            # Simula pipeline como se fosse voz
            self._dashboard.add_conversation("user", text)
            if self._claude_capture_enabled and self._claude:
                self._handle_claude_streaming(text)

        self._dashboard.set_text_callback(_text_from_dashboard)

        # Túnel remoto (ngrok) — opcional
        ngrok_token = os.environ.get("NGROK_TOKEN", "")
        if self._cfg.get("remote", "tunnel_enabled", default=False) or ngrok_token:
            self._tunnel = RemoteTunnel(port=dashboard_port, auth_token=ngrok_token)
            threading.Thread(target=self._tunnel.start, daemon=True, name="ngrok").start()
        else:
            self._tunnel = None

        # Monitor proativo de erros
        self._proactive.start(on_alert=self._on_proactive_alert)

        # MCP em background
        mcp_thread = threading.Thread(target=self._start_mcp, daemon=True, name="mcp-init")
        mcp_thread.start()

        try:
            self._capture.start()
        except Exception as exc:
            logger.error("Falha ao iniciar captura de áudio: %s", exc)
            sys.exit(1)

        self._print_banner()

        init_thread = threading.Thread(
            target=self._load_models, daemon=True, name="model-init"
        )
        init_thread.start()

        while self._running:
            time.sleep(0.1)

        self._cleanup()

    # ── Pipeline de áudio ───────────────────────────────────────────────────────

    def _on_audio(self, audio: np.ndarray, sample_rate: int) -> None:
        if self._transcriber is None:
            logger.warning("Modelos ainda carregando...")
            self._update_status("idle")
            return

        self._update_status("processing")
        t_audio_end = time.monotonic()

        # Detecção de emoção (não bloqueia o pipeline)
        try:
            self._last_emotion = self._emotion_detector.detect(audio, sample_rate)
            if self._last_emotion["state"] != "calm":
                logger.info("[EMOÇÃO] %s (%.0f%%)", self._last_emotion["state"],
                            self._last_emotion["confidence"] * 100)
        except Exception:
            self._last_emotion = None

        # Transcrição
        try:
            result = self._transcriber.transcribe(audio, sample_rate)
        except Exception as exc:
            logger.error("Erro na transcrição: %s", exc)
            self._update_status("error")
            time.sleep(1)
            self._update_status("idle")
            return

        logger.info("[LATÊNCIA] Transcrição: %.2fs", time.monotonic() - t_audio_end)

        if result is None:
            self._update_status("idle")
            return

        text = result.text.strip()

        # Descarta transcrições muito curtas ou claramente com ruído (< 4 chars)
        if len(text) < 4:
            logger.debug("Transcrição descartada (muito curta): %r", text)
            self._update_status("idle")
            return

        self._audit.log_voice_input(
            transcribed_text=text,
            duration_ms=int(result.duration_s * 1000),
            language=result.language,
        )
        logger.info("[VOZ] %r", text)
        self._dashboard.add_conversation("user", text)

        # Salva na memória da sessão
        if self._memory:
            self._memory.save_conversation_turn(
                session_id=self._audit._session_id,
                role="user",
                content=text,
            )

        # Comando para parar o áudio
        if self._is_stop_speaking(text):
            self._audio_player.stop()
            if self._streaming_pipeline:
                self._streaming_pipeline.stop()
            self._update_status("idle")
            return

        # Verifica integrações antes do Claude
        integration_response = self._try_integrations(text)
        if integration_response:
            print(f"  [Jarvis] {integration_response}")
            if self._tts_enabled and self._tts_engine:
                self._speak(integration_response)
            self._update_status("idle")
            return

        # Interpreta comando
        interpreted = self._interpreter.interpret(text)
        if isinstance(interpreted, ControlCommand):
            self._handle_control(interpreted, text, result)
        else:
            self._handle_dictation(interpreted, text, result)

        self._update_status("idle")

    def _try_integrations(self, text: str) -> Optional[str]:
        """Tenta roteamento para Spotify, GitHub ou Google antes de ir ao Claude."""
        text_lower = text.lower()

        # Spotify
        spotify_triggers = ["toca ", "pausa", "parar música", "próxima", "pula ",
                            "anterior", "volta música", "volume ", "que música", "o que está tocando"]
        if any(t in text_lower for t in spotify_triggers) and self._spotify.is_available():
            return self._spotify.handle_voice_command(text)

        # GitHub
        github_triggers = ["pull request", "issue", "commits", "repositório",
                           "status do projeto", "cria issue", "pr aberta"]
        if any(t in text_lower for t in github_triggers) and self._github.is_available():
            return self._github.handle_voice_command(text)

        # Google Calendar / Gmail
        google_triggers = ["agenda", "compromisso", "reunião", "email",
                           "caixa de entrada", "emails não lidos", "próximo evento"]
        if any(t in text_lower for t in google_triggers) and self._google.is_available():
            return self._google.handle_voice_command(text)

        return None

    def _handle_control(self, command: ControlCommand, raw_text: str, result) -> None:
        ctx = {
            "injector": self._injector,
            "last_dictation": self._interpreter.last_dictation,
        }
        msg = self._interpreter.execute_control(command, ctx)
        logger.info("[CONTROLE] %s → %s", command.type.name, msg)
        self._audit.log_control_command(command.type.name, raw_text)
        self._history.add(
            text=raw_text, entry_type="control",
            command=command.type.name, success=True,
            language=result.language,
            inference_ms=int(result.inference_time_s * 1000),
        )
        if msg:
            print(f"  [Controle] {msg}")

    def _handle_dictation(self, text: str, raw_text: str, result) -> None:
        injection_enabled = self._cfg.get("injection", "enabled", default=True)
        if injection_enabled:
            injected = self._injector.inject(text)
            status = "[Injetado]" if injected else "[Falha de injeção]"
            print(f"  {status} {text!r}")

        self._history.add(
            text=text, entry_type="dictation", success=True,
            language=result.language,
            inference_ms=int(result.inference_time_s * 1000),
        )
        print(f"  [Victor] {text!r}")

        if self._claude_capture_enabled and self._claude:
            self._handle_claude_streaming(text)

    def _build_system_prompt(self, user_text: str) -> str:
        """Constrói system prompt enriquecido com memória relevante, emoção e contexto VS Code."""
        parts = []

        # Apenas memórias relevantes à query atual (não todas)
        if self._memory and self._cfg.get("memory", "inject_in_context", default=True):
            relevant = self._memory.get_relevant_memories(user_text, limit=5)
            if relevant:
                parts.append(relevant)

        # Contexto do VS Code (já limitado a 2000 chars em vscode_context.py)
        vscode_ctx = self._vscode.get_context_for_prompt()
        if vscode_ctx:
            parts.append(vscode_ctx)

        # Modificador de tom baseado em emoção
        if self._last_emotion:
            modifier = self._emotion_detector.get_tone_modifier(self._last_emotion["state"])
            if modifier:
                parts.append(modifier)

        return "\n\n".join(parts) if parts else ""

    def _handle_claude_streaming(self, user_text: str) -> None:
        # Increment generation first — any running thread will detect the mismatch
        # and bail at its next guard check.
        self._pipeline_gen += 1
        my_gen = self._pipeline_gen

        # Hard-stop whatever is playing RIGHT NOW before we start anything new.
        # _on_status("recording") only fires on key-DOWN; by the time transcription
        # completes (~2 s) and we arrive here, the previous pipeline may still be
        # running with its _stop_event never set (especially when the key-DOWN
        # arrived while the sentinel `True` was in place and was silently ignored).
        if self._streaming_pipeline is not None:
            self._streaming_pipeline.stop()
        self._audio_player.stop()

        self._streaming_pipeline = _PipelineStarting()

        t_start = time.monotonic()
        chunk_queue: queue.Queue = queue.Queue()
        first_chunk_logged = [False]

        # Roteia para o modelo adequado
        if self._model_router:
            model_id, tier = self._model_router.route(user_text)
            if tier != "haiku":
                logger.info("[ROTEAMENTO] %s → %s", tier, model_id)
            if hasattr(self._claude, "set_model"):
                self._claude.set_model(model_id)

        # Injeta contexto extra no system prompt
        extra_context = self._build_system_prompt(user_text)
        if extra_context and hasattr(self._claude, "set_extra_context"):
            self._claude.set_extra_context(extra_context)

        def on_chunk(chunk: str) -> None:
            if self._pipeline_gen != my_gen:
                return  # superseded — discard chunk, don't feed the abandoned pipeline
            if not first_chunk_logged[0]:
                first_chunk_logged[0] = True
                logger.info("[LATÊNCIA] Claude primeiro chunk: %.2fs", time.monotonic() - t_start)
            chunk_queue.put(chunk)

        def on_done(response: Optional[str]) -> None:
            chunk_queue.put(None)  # always signal end so text_iterator exits cleanly
            if response and self._pipeline_gen == my_gen:
                logger.info("[LATÊNCIA] Claude resposta completa: %.2fs", time.monotonic() - t_start)
                self._audit.log_claude_response(
                    response_text=response,
                    inference_ms=int((time.monotonic() - t_start) * 1000),
                    was_truncated=False,
                )
                if self._memory:
                    self._memory.save_conversation_turn(
                        session_id=self._audit._session_id,
                        role="assistant",
                        content=response,
                    )

        def run_pipeline() -> None:
            # Guard 1 — bail before even calling the API if already superseded
            if self._pipeline_gen != my_gen:
                return

            self._claude.send_streaming_async(user_text, on_chunk, on_done)

            def text_iterator():
                timeout = self._cfg.get("claude_capture", "timeout_seconds", default=60)
                while True:
                    try:
                        chunk = chunk_queue.get(timeout=timeout)
                        if chunk is None:
                            break
                        if self._pipeline_gen != my_gen:
                            break  # superseded while waiting — stop feeding TTS
                        yield chunk
                    except queue.Empty:
                        logger.warning("Timeout aguardando Claude.")
                        break

            # Guard 2 — bail before creating pipeline if superseded while API was in flight
            if self._pipeline_gen != my_gen:
                return

            t_tts_start = [None]

            full_response: list[str] = []

            def on_sentence(sentence: str) -> None:
                if t_tts_start[0] is None:
                    t_tts_start[0] = time.monotonic()
                    logger.info("[LATÊNCIA] Primeira frase TTS: %.2fs", t_tts_start[0] - t_start)
                full_response.append(sentence)
                print(f"  [Jarvis] {sentence}")
                # Atualiza dashboard em tempo real
                self._dashboard.add_conversation("jarvis", sentence, model=self._state.get("model", "") if hasattr(self, "_state") else "")

            if self._tts_enabled and self._tts_engine and self._tts_engine.is_available():
                pipeline = StreamingTTSPipeline(self._tts_engine, self._audio_player)

                # Guard 3 — bail before registering the pipeline object (race-free handoff)
                if self._pipeline_gen != my_gen:
                    return

                self._streaming_pipeline = pipeline
                pipeline.stream(text_iterator(), on_sentence=on_sentence)
            else:
                # TTS indisponível — consome o iterator e só imprime no terminal
                for chunk in text_iterator():
                    pass  # on_chunk já acumula; on_done imprimiu via print

            if t_tts_start[0]:
                logger.info("[LATÊNCIA] Total (fala→áudio): %.2fs", time.monotonic() - t_start)
            # Only clear the reference if we're still the active pipeline
            if self._pipeline_gen == my_gen:
                self._streaming_pipeline = None

        threading.Thread(target=run_pipeline, daemon=True, name="jarvis-pipeline").start()

    def _speak(self, text: str) -> None:
        if not self._tts_engine:
            return
        try:
            audio_bytes = self._tts_engine.synthesize(text)
            self._audio_player.play(audio_bytes)
            self._audit.log_tts_output(spoken_text=text, engine=self._tts_engine.name)
        except Exception as exc:
            logger.error("Erro no TTS: %s", exc)

    def _is_stop_speaking(self, text: str) -> bool:
        import unicodedata, re
        normalized = re.sub(r"[^\w\s]", "",
            unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()
        ).lower().strip()
        return any(p.lower() in normalized for p in self._stop_speaking_phrases)

    def _on_proactive_alert(self, message: str) -> None:
        """Callback do monitor proativo — Jarvis avisa sobre erros detectados."""
        alert_text = f"Victor, detectei um erro: {message}"
        print(f"\n  [Jarvis ⚠] {alert_text}")
        if self._tts_enabled and self._tts_engine and self._tts_engine.is_available():
            self._speak(alert_text)

    # ── Inicialização dos modelos ────────────────────────────────────────────────

    def _load_models(self) -> None:
        print("  Carregando modelos... aguarde.")

        try:
            self._transcriber = Transcriber(self._cfg)
            print("  ✓ Whisper carregado.")
        except Exception as exc:
            logger.error("Falha ao carregar Whisper: %s", exc)
            self._update_status("error")

        if self._tts_enabled:
            try:
                self._tts_engine = create_tts_engine(self._cfg)
                if self._tts_engine.is_available():
                    print(f"  ✓ TTS carregado ({self._tts_engine.name}).")
                else:
                    print("  ⚠ TTS não disponível.")
            except Exception as exc:
                logger.warning("TTS não carregado: %s", exc)

        # Log de integrações disponíveis
        integrations = {
            "Spotify": self._spotify.is_available(),
            "GitHub": self._github.is_available(),
            "Google": self._google.is_available(),
        }
        for name, available in integrations.items():
            print(f"  {'✓' if available else '○'} {name}: {'ativo' if available else 'não configurado'}")

        if self._transcriber:
            mode = self._cfg.get("activation", "mode", default="push_to_talk")
            if mode == "wake_word":
                words = self._cfg.get("activation", "wake_words", default=["jarvis"])
                print(f"\n  Sistema pronto! Diga {' / '.join(words)} para ativar.\n")
            else:
                key = self._cfg.get("activation", "push_to_talk_key", default="alt")
                print(f"\n  Sistema pronto! Segure {key.upper()} e fale.\n")

    def _start_mcp(self) -> None:
        results = self._mcp.start_all()
        if results:
            for name, ok in results.items():
                print(f"  {'✓' if ok else '✗'} MCP {name}: {'ativo' if ok else 'falhou'}")

    # ── Banner ──────────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        mode = self._cfg.get("activation", "mode", default="push_to_talk")
        key = self._cfg.get("activation", "push_to_talk_key", default="alt")
        model = self._cfg.get("transcription", "model", default="small")
        tts_engine = self._cfg.get("tts", "engine", default="edge")
        dashboard_port = self._cfg.get("ui", "dashboard_port", default=7432)

        activation_label = (
            f"wake word ({', '.join(self._cfg.get('activation', 'wake_words', default=['jarvis']))})"
            if mode == "wake_word"
            else f"push-to-talk ({key.upper()})"
        )
        routing_label = "Haiku/Sonnet (automático)" if self._model_router else "fixo"

        print(f"\n{'='*62}")
        print("  J A R V I S  —  Assistente Pessoal de Victor Germano")
        print(f"{'='*62}")
        print(f"  Ativação:      {activation_label}")
        print(f"  STT:           Whisper {model}")
        print(f"  TTS:           {tts_engine}")
        print(f"  Modelo:        {routing_label}")
        print(f"  Memória:       {'ativa' if self._memory else 'desativada'}")
        print(f"  Dashboard:     http://localhost:{dashboard_port}")
        print(f"  Emoção:        ativa")
        print(f"{'='*62}")
        print("  Ctrl+C para sair | 'para de falar' para silenciar")
        print()

    # ── Saída ───────────────────────────────────────────────────────────────────

    def _signal_handler(self, signum, frame) -> None:
        logger.info("Sinal %d recebido. Encerrando...", signum)
        self._shutdown()

    def _shutdown(self) -> None:
        self._running = False

    def _cleanup(self) -> None:
        logger.info("Encerrando sistema...")
        self._audio_player.stop()
        if self._memory:
            self._memory.close()
        for obj in [self._capture, self._mcp, self._tray, self._proactive, self._dashboard]:
            try:
                obj.stop()
            except Exception:
                pass
        logger.info("Sistema encerrado.")


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S — Assistente Pessoal de Voz")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--model", default=None, help="Modelo Whisper (tiny/base/small/medium)")
    parser.add_argument("--no-tts", action="store_true")
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        print_microphones()
        sys.exit(0)

    if args.debug:
        configure_logging(level="DEBUG")

    app = VoiceProgrammer(config_path=args.config)

    if args.model:
        app._cfg.set(args.model, "transcription", "model")
    if args.no_tts:
        app._cfg.set(False, "tts", "enabled")
    if args.no_capture:
        app._cfg.set(False, "claude_capture", "enabled")
    if args.debug:
        app._cfg.set("DEBUG", "logging", "level")

    app.run()


if __name__ == "__main__":
    main()

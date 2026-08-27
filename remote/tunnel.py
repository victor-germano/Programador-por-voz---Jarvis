"""
Túnel remoto via ngrok — expõe o Jarvis para acesso externo (celular, etc).

Uso:
  python remote/tunnel.py          → inicia túnel na porta 7432
  python remote/tunnel.py --port X → porta personalizada

Requer: pip install pyngrok
Conta gratuita em: https://ngrok.com (necessário para URL estável)
"""

import subprocess
import sys
import threading
import time
import os
from typing import Optional

from logging_setup import get_logger
logger = get_logger(__name__)


class RemoteTunnel:
    def __init__(self, port: int = 7432, auth_token: str = "") -> None:
        self._port = port
        self._auth_token = auth_token or os.environ.get("NGROK_TOKEN", "")
        self._public_url: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._tunnel = None

    @property
    def public_url(self) -> Optional[str]:
        return self._public_url

    def start(self) -> Optional[str]:
        """Inicia o túnel e retorna a URL pública."""
        try:
            from pyngrok import ngrok, conf

            if self._auth_token:
                conf.get_default().auth_token = self._auth_token

            self._tunnel = ngrok.connect(self._port, "http")
            self._public_url = self._tunnel.public_url.replace("http://", "https://")
            logger.info("Túnel remoto ativo: %s", self._public_url)
            self._print_access_info()
            return self._public_url

        except ImportError:
            logger.warning("pyngrok não instalado. Execute: pip install pyngrok")
            return None
        except Exception as e:
            logger.warning("Falha ao iniciar túnel ngrok: %s", e)
            return None

    def stop(self) -> None:
        try:
            from pyngrok import ngrok
            ngrok.disconnect(self._tunnel.public_url)
        except Exception:
            pass

    def _print_access_info(self):
        url = self._public_url
        mobile_url = f"{url}/mobile"
        print(f"""
╔══════════════════════════════════════════════════════╗
║           J.A.R.V.I.S — ACESSO REMOTO ATIVO          ║
╠══════════════════════════════════════════════════════╣
║  Dashboard : {url:<40}║
║  Mobile    : {mobile_url:<40}║
╚══════════════════════════════════════════════════════╝

  📱 Abra o link Mobile no seu celular para falar com o Jarvis
  🔒 URL válida enquanto o Jarvis estiver rodando
""")

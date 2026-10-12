# Jarvis — Assistente Pessoal de Voz

> Assistente de IA com voz bidirecional, integrado ao ambiente de desenvolvimento (VS Code), com latência de aproximadamente 1 segundo entre o comando falado e o início da resposta.

---

## Sobre o Projeto

Assistente pessoal de IA com reconhecimento e síntese de voz bidirecional, rodando localmente, desenvolvido em Python com a Claude API (Anthropic). O sistema captura o comando por push-to-talk, transcreve, processa com IA e responde em áudio.

Além de executar comandos, o sistema é projetado para opinar sobre decisões técnicas, sugerir alternativas e sinalizar riscos — não apenas confirmar instruções.

Exemplo de interação:

```
[Push-to-talk: Ctrl+R]
Usuário: "Jarvis, essa função está grande demais."

[~1s depois, resposta em voz]
Jarvis: "Ela tem três responsabilidades distintas.
         Eu quebraria em duas. Quer que eu faça agora?"
```

---

## Funcionalidades

| Funcionalidade | Detalhe |
|---|---|
| Voz bidirecional | Resposta em streaming por frase, com latência de ~1s |
| Roteamento de modelos | Seleção automática entre Haiku, Sonnet e Opus por complexidade da tarefa |
| Memória persistente | Histórico de conversas e decisões entre sessões (SQLite) |
| Detecção de tom de voz | Ajusta a resposta conforme sinais de estresse, entusiasmo ou cansaço |
| Contexto do VS Code | Usa o arquivo em edição como contexto da conversa |
| Integração com Spotify | Controle de reprodução por comando de voz |
| Integração com Google Calendar/Gmail | Consulta de agenda e caixa de entrada |
| Integração com GitHub | Consulta de PRs, commits e criação de issues |
| Monitor proativo | Detecta erros no terminal e notifica por voz |
| Dashboard web | Interface local (porta 7432) com histórico de conversas |
| Overlay visual | Indicador visual de escuta ativa |

---

## Pipeline

```
Microfone (push-to-talk: Ctrl+R)
   |
faster-whisper — transcrição local (~0.3s)
   |
Detecção de tom de voz
   |
Roteamento de modelo — Haiku / Sonnet / Opus (automático)
   |
Claude API (Anthropic) — geração da resposta (~0.8s)
   |
Humanizer — remove markdown, bullets e blocos de código da resposta em voz
   |
Edge TTS (pt-BR-AntonioNeural) — síntese em streaming por frase
   |
Saída de áudio

Latência total: ~1s entre o fim da fala e o início da resposta.
```

---

## Tecnologias

| Camada | Tecnologia |
|---|---|
| Linguagem | Python |
| IA | Claude API (Anthropic) |
| Reconhecimento de voz | faster-whisper |
| Síntese de voz | Edge TTS |
| Memória | SQLite |
| Integração Spotify | Spotify API |
| Integração Google | Google Calendar/Gmail API |
| Integração GitHub | GitHub API |
| Interface | tkinter |
| Dashboard | Flask |
| Hotkey global | pynput |

Suíte de 96 testes automatizados. Setup de integrações via script interativo (`setup_integrations.py`).

---

## Instalação

```powershell
git clone https://github.com/victor-germano/jarvis-assistente-de-voz
cd jarvis-assistente-de-voz
pip install -r requirements.txt
set ANTHROPIC_API_KEY=sk-ant-...
py main.py
```

### Configurar integrações

```powershell
py setup_integrations.py
```

Wizard interativo que configura Spotify, GitHub e Google.

---

## Arquitetura

```
jarvis/
├── main.py                    # Orquestrador principal
├── config.yaml                # Configuração
├── CLAUDE.md                  # Definição de comportamento do assistente
├── setup_integrations.py      # Wizard de configuração
├── audio_capture/              # Captura de áudio e push-to-talk
├── transcription/               # faster-whisper
├── response_capture/            # Cliente da API Anthropic
├── model_router/                 # Roteamento Haiku/Sonnet/Opus
├── memory/                      # Persistência (SQLite)
├── emotion/                     # Detecção de tom de voz
├── integrations/                # Spotify, GitHub, Google, VS Code
├── proactive/                   # Monitor de erros no terminal
├── dashboard/                   # Dashboard web (Flask)
├── tts/                         # Síntese de voz (Edge TTS)
├── tray/                        # Bandeja do sistema e overlay
└── tests/                       # Suíte de testes (96 testes)
```

---

## Autor

**Victor Germano** — Desenvolvedor Web Full Stack, IA & Automação

---

## Licença

MIT License

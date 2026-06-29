# Hermes Live Transcript

Real-time web viewer for Hermes Agent conversations. Reads directly from Hermes' SQLite session store and displays messages in a dark-themed browser UI with automatic polling.

## Scopo

Mostrare in tempo reale (o quasi) la conversazione in corso tra l'utente e Hermes Agent, includendo messaggi di agenti esterni (Claude Code, Codex, Antigravity/Gemini) intercettati dall'Agent Bus.

Utile per:
- **Monitoraggio visivo** della conversazione mentre Hermes lavora
- **Debug** — vedere esattamente cosa Hermes ha restituito e quali tool ha chiamato
- **Condivisione** — mostrare la conversazione ad altri agenti umani o AI
- **Osservabilità** — verificare lo stato degli agenti esterni (vivi/morti) in un colpo d'occhio

## Architettura

```
┌─────────────────────────────────────────────────┐
│                 Browser (localhost:8800)          │
│  poll /api/current?after=N&bus_after=M&sid=...   │
│  poll /api/bus/status  (agent liveness)          │
└──────────────────┬──────────────────────────────┘
                   │ HTTP (Python HTTPServer)
┌──────────────────▼──────────────────────────────┐
│              server.py (port 8800)               │
│                                                  │
│  GET /            → HTML page (inline CSS + JS)  │
│  GET /api/current → JSON (session messages)      │
│  GET /api/status  → JSON (session alive check)   │
│  GET /api/bus/status → JSON (agent liveness)     │
└──────┬──────────────────────────┬───────────────┘
       │                          │
       ▼                          ▼
┌──────────────┐      ┌──────────────────────┐
│  state.db     │      │  agent-bus-log.db     │
│  ~/.hermes/   │      │  ~/.hermes/           │
│  state.db     │      │  agent-bus-log.db     │
└──────────────┘      └──────────────────────┘
```

## Servizi utilizzati e percorsi filesystem

| Risorsa | Percorso | Descrizione |
|---------|----------|-------------|
| **state.db** | `~/.hermes/state.db` | Database SQLite delle sessioni Hermes. Contiene tabelle `sessions`, `messages`, `messages_fts`. Lettura sola da parte del server. |
| **agent-bus-log.db** | `~/.hermes/agent-bus-log.db` | Database SQLite dei messaggi tra agenti (Agent Bus). Tabella `bus_messages` con agent, direction, content. Opzionale — il server funziona anche senza. |
| **LaunchAgent plist** | `~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist` | Configurazione launchd per avvio persistente. KeepAlive + RunAtLoad. |
| **Log di servizio** | `~/.hermes/logs/live-transcript.log` | stdout/stderr del processo server (vuoto in condizioni normali, errori vanno qui). |
| **Agent Telemetry** | Porta 9900 — `~/Software/scripts-ai/agent_telemetry.py` | Servizio HTTP che taila i log di Claude Code, Codex, Antigravity. Usato da `/api/bus/status`. |

## File del progetto

| File | Descrizione |
|------|-------------|
| **server.py** | Server HTTP + pagina web. Unico file Python — contiene backend API + frontend HTML/CSS/JS inline. |
| **log-bus.py** | Utility CLI per scrivere messaggi su `agent-bus-log.db`. Usata da script esterni che vogliono far apparire messaggi di agenti nella UI. |
| **archive-old-sessions.py** | Utility per archiviare sessioni Hermes vecchie (>N giorni) in un DB separato, riducendo la dimensione di state.db. Usa la backup API di SQLite per includere correttamente eventuali contenuti WAL; l'archivio finale contiene solo le sessioni vecchie. Uso: `python3 archive-old-sessions.py --days 6`. |
| **com.fausto.hermes-live-transcript.plist** | LaunchAgent plist per launchd. Copia locale (quella attiva è in `~/Library/LaunchAgents/`). |

## API

### `GET /api/current`

Restituisce i messaggi della sessione Hermes più recente di oggi (escluse sessioni cron).

| Parametro | Default | Descrizione |
|-----------|---------|-------------|
| `after` | 0 | ID minimo dei messaggi da restituire (per polling incrementale) |
| `bus_after` | 0 | ID minimo dei messaggi bus da restituire |
| `limit` | 60 | Numero massimo di messaggi (solo su poll iniziale) |
| `session_id` | auto | Sessione a cui pinnarsi (per evitare switching) |

Risposta:
```json
{
  "session_id": "20260629_065835_af2696",
  "session": { "title": "...", "message_count": 150, "started_at": 1782709156.12 },
  "messages": [
    { "id": 7125, "role": "assistant", "display": "hermes", "content": "...", "timestamp": 1782710123.45 },
    { "id": -13, "bus_id": 13, "display": "codex", "content": "← from ...", "timestamp": 1782578640.96, "is_bus": true }
  ],
  "last_id": 7125,
  "last_transcript_id": 7125,
  "last_bus_id": 13
}
```

### `GET /api/status`

Restituisce `{"session_id": "...", "ok": true}` se esiste una sessione attiva oggi.

### `GET /api/bus/status`

Restituisce lo stato di vita degli agenti esterni (letto da Agent Telemetry su port 9900).

## UI / Comportamento

- **Polling automatico** ogni N ms (configurabile via `--dev` = 1s, default = 3s)
- **Messaggi ordinati dal più recente in alto** (`flex-direction: column-reverse`)
- **Collassamento automatico** dei messaggi lunghi (>500 caratteri) — click per espandere
- **Badge agenti** in alto con pallino verde (vivo) / rosso (morto) — hover per comando tmux attach, click per copiare
- **Limit visibile**: ultimi 60 messaggi (i più vecchi vengono rimossi dal DOM)
- **Auto-scroll**: i nuovi messaggi appaiono in cima automaticamente

## Problemi noti / Issue

### 1. Ritardo nella comparsa dei nuovi messaggi

Hermes scrive i messaggi su `state.db` incrementalmente durante il conversation loop. C'è un ritardo naturale (∼secondi) tra quando Hermes genera una risposta e quando viene persistita nel DB. La UI polla ogni 1-3 secondi ma non può mostrare messaggi che non sono ancora stati scritti.

**Workaround**: nessuno — è un limite intrinseco dell'architettura state.db-async. Il poll veloce (`--dev`, 1s) minimizza la percezione.

### 2. Session switching involontario

Le sessioni cron (`cron_*`) vengono escluse dalla selezione (`NOT LIKE 'cron_%'`), ma se l'utente avvia manualmente una nuova sessione Hermes CLI, il server passa alla nuova sessione al prossimo poll. La UI lato client si pinnava alla sessione scelta, ma un hard refresh fa ripartire dalla sessione più recente.

**Fix applicato**: cache `_pinned_session_id` lato server + parametro `session_id` nelle richieste AJAX.

### 3. Messaggi vuoti (assistant-only-tool-calls)

Quando Hermes risponde con solo chiamate a strumenti (es. `terminal`) e nessun contenuto testuale, il campo `content` in state.db è vuoto. La UI mostrava un messaggio senza testo.

**Fix applicato**: filtro SQL `AND NOT (role='assistant' AND (content IS NULL OR content=''))` in `get_messages()`.

### 4. Agent bus merge / polling inconsistente

I messaggi dell'Agent Bus (con ID negativo, `is_bus=true`) vengono fusi con i messaggi transcript ordinando per timestamp. Se due messaggi hanno lo stesso timestamp, l'ordine è indeterminato. Inoltre, il merge poteva causare duplicati se il trim tagliava via messaggi transcript e il `last_transcript_id` retrocedeva; un altro caso problematico era l'apertura della pagina prima dell'arrivo di qualunque messaggio bus, perché `last_bus_id` restava 0 e i poll successivi non interrogavano più il DB bus.

**Fix applicato**: estrazione di `last_transcript_id` e `last_bus_id` prima del trim, poll incrementale del bus anche quando il client non ha ancora un `last_bus_id`, e stato client `initialized` separato da `last_transcript_id`.

### 5. Archiviazione SQLite WAL

`state.db` usa SQLite e può avere contenuti recenti nel file WAL. Copiare solo `state.db` a livello filesystem rischia un archivio incompleto.

**Fix applicato**: `archive-old-sessions.py` usa `sqlite3.Connection.backup()` e poi pota l'archivio lasciando solo le sessioni vecchie. Le tabelle FTS vengono aggiornate dai trigger `messages_*`, non cancellate manualmente.

### 6. Assenza di ricarica automatica del JS

Le modifiche a `server.py` (in particolare alla parte HTML/JS inline) richiedono un hard refresh del browser (`Cmd+Shift+R`) perché la pagina HTML è generata dal server ma potrebbe essere cacheata dal browser.

**Nota**: il poll AJAX va sempre all'API corrente, quindi i dati sono sempre freschi. Solo il template HTML/JS iniziale può essere stale.

### 7. Singolo thread

Il server usa `http.server.HTTPServer` di Python che è single-thread. In condizioni di uso intenso (decine di richieste al secondo), potrebbe diventare un collo di bottiglia. Per il carico attuale (1-2 utenti, poll ogni 1-3s) è ampiamente sufficiente.

## Dev mode

```bash
python3 server.py 8800 --dev
```

Abilita:
- Poll interval 1s invece di 3s
- Console log nel browser: `[Hermes Live] DEV MODE`
- Ideale per sviluppo e debug

## LaunchD

Il servizio è gestito da launchd con KeepAlive:

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist

# Start (o riavvio dopo modifiche)
launchctl load ~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist

# Log
tail -f ~/.hermes/logs/live-transcript.log
```

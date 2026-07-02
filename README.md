# Hermes Live Transcript

Web viewer locale per seguire quasi in tempo reale la sessione Hermes corrente. Il server legge `~/.hermes/state.db`, fonde i messaggi Hermes con l'eventuale log dell'Agent Bus e serve una UI scura su `http://127.0.0.1:8800`.

## Scopo

Mostrare la conversazione in corso tra utente e Hermes Agent, includendo quando disponibili i messaggi di agenti esterni intercettati dall'Agent Bus.

Utile per:

- **Monitoraggio visivo** della sessione mentre Hermes lavora
- **Debug** dei messaggi persistiti da Hermes e delle risposte degli agenti esterni
- **Condivisione** della conversazione con altri agenti umani o AI
- **Osservabilità** degli agenti esterni tramite badge di liveness

## Architettura

```text
┌────────────────────────────────────────────────────┐
│              Browser (127.0.0.1:8800)              │
│  GET  /api/current?after=N&bus_after=M&session_id= │
│  GET  /api/bus/status                              │
│  GET  /api/agenttalk/backlog                       │
│  POST /api/send                                    │
│  POST /api/archive                                 │
└───────────────────────┬────────────────────────────┘
                        │ HTTP locale
┌───────────────────────▼────────────────────────────┐
│                 server.py (port 8800)              │
│          Python ThreadingHTTPServer                │
│                                                     │
│  GET  /                 HTML/CSS/JS inline         │
│  GET  /api/current      transcript + bus JSON      │
│  GET  /api/status       sessione corrente          │
│  GET  /api/bus/status   liveness agenti            │
│  GET  /api/agenttalk/   proxy backlog AgentTalk    │
│  POST /api/send         proxy verso Hermes API     │
│  POST /api/archive      salva Markdown locale      │
└───────────────┬───────────────────┬────────────────┬────────────────┘
                │                   │                │
                ▼                   ▼                ▼
┌────────────────────────┐  ┌────────────────────────┐  ┌────────────────────────┐
│ ~/.hermes/state.db     │  │ ~/.hermes/agent-bus-   │  │ AgentTalk orchestrator │
│ sessions, messages     │  │ log.db / bus_messages  │  │ /api/backlog           │
└────────────────────────┘  └────────────────────────┘  └────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────┐
│ Hermes API server, default http://127.0.0.1:8642   │
│ usato solo da /api/send                            │
└────────────────────────────────────────────────────┘
```

## Servizi e percorsi

| Risorsa | Percorso / default | Descrizione |
| --- | --- | --- |
| Database Hermes | `~/.hermes/state.db` | SQLite letto dal server. Usa `sessions` e `messages`; ignora ruoli `tool` e `session_meta` e gli assistant message vuoti. |
| Agent Bus log | `~/.hermes/agent-bus-log.db` | SQLite opzionale. Se esiste, il server legge `bus_messages` e li fonde nel transcript in base al timestamp. |
| Agent sessions | `~/.hermes/agent-sessions.json` | Opzionale. Usato per mostrare il comando `tmux attach` nei badge agenti. |
| Agent Telemetry | `http://127.0.0.1:9900/agents` | Sorgente di liveness per `/api/bus/status`. Se non risponde, la barra agenti resta vuota. |
| Hermes API base URL | `HERMES_API_BASE_URL` o `http://127.0.0.1:8642` | Endpoint usato dal proxy `/api/send`. |
| Progetto corrente | `HERMES_LIVE_PROJECT_NAME`, `HERMES_LIVE_PROJECT_PATH` | Metadati del progetto di sviluppo passati a Hermes quando la UI crea una nuova sessione. Include i linchpin docs `AGENT.md` e `design/collaboration-workflow.md`. |
| AgentTalk API base URL | `AGENTTALK_API_BASE_URL` o `http://127.0.0.1:3741` | Endpoint usato dal proxy `/api/agenttalk/backlog` per mostrare il backlog nella UI. |
| API key Hermes | vedi sotto | Letta solo lato server; non viene inserita nell'HTML servito al browser. |
| LaunchAgent plist | `~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist` | Configurazione launchd installata. Il file nel repo è la copia sorgente. |
| Log servizio | `~/.hermes/logs/live-transcript.log` | stdout/stderr del processo launchd. |
| Archivio transcript visibili | `~/.hermes/live-transcript-archives/` | Creato dal pulsante **Archive** o da `POST /api/archive`. Salva Markdown leggibile. |
| Archivio sessioni | `~/.hermes/archived/` | Creato da `archive-old-sessions.py`. |

Ordine effettivo di lookup della API key:

1. env `HERMES_LIVE_TRANSCRIPT_API_KEY`
2. env `API_SERVER_KEY`
3. `API_SERVER_KEY` in `~/.hermes/.env`
4. `gateway.platforms.api_server.extra.key` in `~/.hermes/config.yaml`
5. `~/.hermes/live-transcript-api-key`

## File del progetto

| File | Descrizione |
| --- | --- |
| `server.py` | Server HTTP, API JSON e frontend inline. Avvia su `127.0.0.1`, porta `8800` di default. |
| `log-bus.py` | Utility CLI per inserire righe in `~/.hermes/agent-bus-log.db`. Si aspetta che la tabella `bus_messages` esista gia. |
| `archive-old-sessions.py` | Utility per archiviare sessioni Hermes piu vecchie di `--days`. Usa `sqlite3.Connection.backup()` per includere il WAL, poi pota archivio e DB principale. |
| `com.fausto.hermes-live-transcript.plist` | LaunchAgent per avviare `server.py 8800` con launchd. |

## Esecuzione

```bash
python3 server.py
```

Porta esplicita:

```bash
python3 server.py 8800
```

Dev mode:

```bash
python3 server.py 8800 --dev
```

In dev mode la UI polla ogni 1 secondo invece di 3 e stampa un log nel browser: `[Hermes Live] DEV MODE`.

Configurazione progetto/backlog via env:

```bash
HERMES_LIVE_PROJECT_NAME=AgentTalk \
HERMES_LIVE_PROJECT_PATH=/Users/fausto/Software/AgentTalk \
AGENTTALK_API_BASE_URL=http://127.0.0.1:3741 \
python3 server.py 8800 --dev
```

Per popolare la tab **Backlog**, il backend AgentTalk deve essere in ascolto e servire `GET /api/backlog`, ad esempio dal repo AgentTalk:

```bash
cd /Users/fausto/Software/AgentTalk
npm run backend
```

## API

### `GET /api/current`

Restituisce i messaggi della sessione Hermes corrente. Al primo poll sceglie la sessione non-cron piu recente iniziata oggi; nei poll successivi usa la sessione pinnata o il `session_id` esplicito passato dal client.

Parametri:

| Parametro | Default | Descrizione |
| --- | --- | --- |
| `after` | `0` | ID minimo dei messaggi Hermes da restituire. |
| `bus_after` | `0` | ID minimo dei messaggi Agent Bus da restituire. |
| `limit` | `60` | Limite applicato al poll iniziale. |
| `session_id` | auto | Sessione Hermes da mantenere durante il polling. |

Risposta tipica:

```json
{
  "session_id": "20260629_065835_af2696",
  "session": {
    "title": "...",
    "message_count": 150,
    "started_at": 1782709156.12,
    "ended_at": 0,
    "end_reason": "",
    "source": ""
  },
  "messages": [
    {
      "id": 7125,
      "role": "assistant",
      "display": "hermes",
      "content": "...",
      "timestamp": 1782710123.45
    },
    {
      "id": -13,
      "bus_id": 13,
      "display": "codex",
      "content": "← from ...",
      "timestamp": 1782710124.1,
      "is_bus": true
    }
  ],
  "last_id": 7125,
  "last_transcript_id": 7125,
  "last_bus_id": 13
}
```

### `GET /api/status`

Restituisce:

```json
{"session_id": "...", "ok": true}
```

`ok` e `false` quando non esiste una sessione non-cron iniziata oggi.

### `GET /api/bus/status`

Legge `http://127.0.0.1:9900/agents` e restituisce:

```json
{"agents": [{"name": "codex", "type": "codex", "alive": true, "tmux_attach": "tmux attach -t ..."}]}
```

Il server:

- nasconde agenti `agenttest` e `smtest`
- mappa `agy` a tipo/display `gemini`
- nasconde agenti morti se l'ultima attivita e piu vecchia di 20 minuti
- aggiunge `tmux_attach` quando trova una sessione in `~/.hermes/agent-sessions.json`

### `POST /api/send`

Invia un messaggio a Hermes passando dal proxy locale.

Body:

```json
{"session_id": "20260629_065835_af2696", "message": "..."}
```

Comportamento effettivo:

- rifiuta body vuoti, JSON non oggetto, messaggi non stringa e messaggi oltre 20.000 caratteri
- usa `session_id`, poi la sessione pinnata, poi la sessione corrente di oggi
- se la sessione indicata non esiste, risponde `404`
- se non c'e una sessione valida o quella trovata e conclusa, crea una nuova sessione Hermes con titolo `<project-name> - YYYY-MM-DD HH:MM:SS`
- passa a Hermes nome e path del progetto tramite `system_prompt` della sessione e `instructions` del primo turno
- inoltra il messaggio a `POST {HERMES_API_BASE_URL}/api/sessions/{session_id}/chat/stream`
- risponde subito con `{"ok": true, "queued": true, "session_id": "..."}` e invia a Hermes in un thread background

Nota: se la UI non ha una `sessionId` attiva, chiama comunque `/api/send`; il server crea una nuova sessione Hermes e restituisce il nuovo `session_id`.

### `POST /api/archive`

Salva su file i messaggi attualmente visibili nella UI. Questo endpoint non rilegge `state.db`: riceve dal browser la lista dei nodi `.msg` presenti nel DOM, quindi puo includere anche messaggi live o pending non ancora persistiti da Hermes.

Body:

```json
{
  "session_id": "20260629_065835_af2696",
  "filename": "hermes-live-transcript - 2026-07-01 06-15-00.md",
  "messages": [
    {
      "id": 7125,
      "role": "assistant",
      "display": "hermes",
      "content": "...",
      "timestamp": 1782710123.45
    }
  ]
}
```

Comportamento:

- scrive un file Markdown in `~/.hermes/live-transcript-archives/`
- sanitizza il nome file e aggiunge `.md` se manca
- se il browser non passa un nome file, usa `<project-name> - YYYY-MM-DD HH-MM-SS.md`
- se il file esiste gia, aggiunge un suffisso con archive id
- limita il body a 1 MiB e i messaggi a 200
- include nell'archivio `session_id`, nome progetto, path progetto, timestamp del primo e ultimo messaggio
- risponde con path, filename, archive id e numero messaggi salvati

### `GET /api/agenttalk/backlog`

Proxy locale verso `GET {AGENTTALK_API_BASE_URL}/api/backlog`. Per default AgentTalk restituisce solo item attivi (`doing` e `todo`); `GET /api/agenttalk/backlog?all=true` inoltra `?all=true` e restituisce l'intero backlog. Restituisce i campi AgentTalk `items`, `warnings`, `generatedAt` e `total` quando presente, piu `ok` e `agenttalk_url`. Se AgentTalk non e in ascolto, risponde `502` con un errore leggibile dalla tab **Backlog**.

Risposta tipica:

```json
{
  "ok": true,
  "agenttalk_url": "http://127.0.0.1:3741/api/backlog",
  "generatedAt": "2026-07-01T19:05:00.000Z",
  "total": 9,
  "warnings": [],
  "items": [
    {
      "id": "BL-001",
      "status": "doing",
      "date": "2026-07-01",
      "epic": "M13",
      "promotedTo": null,
      "tags": ["ui", "backlog"],
      "title": "Example backlog item",
      "bodyMarkdown": "- [doing] **Example backlog item** - details..."
    }
  ]
}
```

Errore quando AgentTalk non e disponibile:

```json
{
  "ok": false,
  "error": "AgentTalk backlog API unavailable",
  "detail": "<urlopen error [Errno 61] Connection refused>",
  "agenttalk_url": "http://127.0.0.1:3741/api/backlog"
}
```

## UI / comportamento

- Poll automatico del transcript: 3s default, 1s con `--dev`
- Poll della barra agenti ogni 5s
- Messaggi ordinati con i piu recenti in alto (`flex-direction: column-reverse`)
- Massimo visibile lato client: 60 messaggi, con piccolo margine tecnico durante il trim
- Collasso automatico del contenuto oltre 500 caratteri; click sul contenuto per espandere o richiudere
- Pulsante **Auto-scroll** per saltare in cima quando arrivano nuovi messaggi
- Pulsante **Clear** che svuota solo il DOM locale; non cancella dati dai DB
- Pulsante **Archive** che chiede un nome file e salva i messaggi attualmente visibili in `~/.hermes/live-transcript-archives/`
- Tab **Backlog** che legge il backlog corrente da AgentTalk, mostra l'endpoint API configurato, conteggi per stato, warnings e item raggruppati per epic/spike, e si aggiorna ogni 30s quando e attiva. Le descrizioni sono espandibili; la UI mostra per default gli item attivi restituiti da AgentTalk (`doing` e `todo`), usa **Show all** per richiedere `?all=true`, ordina `doing` prima di `todo`, e evidenzia il primo item attivo come corrente/prossimo.
- Sidebar **Send to Hermes** con **Send** o `Cmd/Ctrl+Enter` per inviare il testo cosi com'e; **Start** sta a destra di Send, aggiunge i metadati del progetto al messaggio, ed e disabilitato dopo l'avvio della sessione
- Messaggio pending locale dopo l'invio, rimosso quando il corrispondente messaggio `human` compare in `state.db`
- Badge agenti con pallino verde/rosso; hover sui badge con sessione tmux per mostrare il comando, click per copiarlo

## Agent Bus

`log-bus.py` inserisce messaggi nel DB bus:

```bash
python3 log-bus.py to claude "message text"
python3 log-bus.py from codex "response text"
```

Se il contenuto non viene passato come argomento, lo legge da stdin:

```bash
printf 'long message\n' | python3 log-bus.py from codex
```

Il server mostra i messaggi bus come ID negativi (`id = -bus_id`) e contenuto prefissato con `→ to` o `← from`.

## Archiviazione sessioni

Dry run:

```bash
python3 archive-old-sessions.py --days 6 --dry-run
```

Archiviazione reale:

```bash
python3 archive-old-sessions.py --days 6
```

Lo script:

1. trova sessioni con `started_at` precedente al cutoff
2. crea un backup SQLite consistente in `~/.hermes/archived/archive-pre-YYYY-MM-DD.db`
3. pota l'archivio lasciando solo le sessioni vecchie
4. elimina dal DB principale messaggi e sessioni archiviate
5. esegue `VACUUM` e `PRAGMA wal_checkpoint(TRUNCATE)`

Le tabelle FTS non vengono manipolate direttamente; sono aggiornate dai trigger della tabella `messages`.

## Launchd

Il servizio e gestito da launchd con `RunAtLoad` e `KeepAlive`.

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist

# Start / reload dopo modifiche al plist
launchctl load ~/Library/LaunchAgents/com.fausto.hermes-live-transcript.plist

# Log
tail -f ~/.hermes/logs/live-transcript.log
```

Il plist nel repo avvia:

```text
/usr/local/bin/python3 /Users/fausto/Software/scripts-ai/hermes-live-transcript/server.py 8800
```

## Problemi noti

### Ritardo nella comparsa dei nuovi messaggi

La UI puo mostrare solo cio che Hermes ha gia persistito in `state.db`. Se Hermes sta ancora generando o non ha ancora scritto il turno nel DB, il poll non puo leggerlo. `--dev` riduce l'intervallo di polling ma non elimina il ritardo di persistenza.

### Session switching

Il primo poll sceglie la sessione non-cron piu recente iniziata oggi. Dopo il caricamento, client e server usano una sessione pinnata tramite `session_id`, quindi i poll incrementali non dovrebbero saltare a una nuova sessione. Un hard refresh o un restart del servizio riparte pero dalla sessione piu recente di oggi; se quella sessione ha solo due messaggi, la UI mostrera solo quei due anche se esistono sessioni precedenti piu lunghe.

### Cache HTML/JS

Le modifiche all'HTML/JS inline in `server.py` richiedono restart del processo server e hard refresh del browser (`Cmd+Shift+R`). Un hard refresh da solo ricarica solo cio che il processo gia in esecuzione sta servendo. Le API JSON restano fresche perche vengono interrogate a ogni poll.

### Dipendenze esterne opzionali

Il transcript funziona anche senza `agent-bus-log.db`, Agent Telemetry o `agent-sessions.json`. In quel caso mancano solo messaggi bus, liveness agenti o comandi tmux nei badge.

## Piano: live preview prima del commit DB

Questa sezione descrive una possibile evoluzione, non il comportamento attuale.

### Contesto

`server.py` legge `state.db` con connessioni fresche a ogni poll. Quando Hermes ha gia chiamato `SessionDB.append_message(...)` e il commit SQLite e completato, il messaggio e visibile al poll successivo. Il problema di latenza non sembra quindi essere un mancato flush del WAL: Hermes usa gia transazioni esplicite con commit per gli append dei messaggi.

La latenza osservata nasce prima del commit: il messaggio esiste in memoria, o come delta di streaming, ma non e ancora stato materializzato in `state.db`. Per vedere quei contenuti subito, la UI deve riceverli da un canale live e poi riconciliarli con il DB quando arrivano i record canonici.

### Obiettivo

Mostrare subito nella UI i messaggi generati da Hermes, senza cambiare la semantica di persistenza di Hermes e senza rendere `state.db` meno canonico.

Il DB resta la fonte definitiva. Il canale live serve solo per preview/pending UI.

### V1 consigliata: usare lo stream Hermes esistente

Per i messaggi inviati dalla sidebar di questa app, la soluzione piu additiva e non richiede modifiche interne a Hermes:

1. `POST /api/send` in `server.py` continua a ricevere il messaggio dal browser.
2. Invece di inoltrare a `POST {HERMES_API_BASE_URL}/api/sessions/{session_id}/chat`, il worker background chiama `POST {HERMES_API_BASE_URL}/api/sessions/{session_id}/chat/stream`.
3. `server.py` legge lo stream SSE prodotto da Hermes.
4. Gli eventi SSE vengono salvati in un buffer in memoria keyed by `session_id`.
5. `GET /api/current` fonde messaggi DB, messaggi Agent Bus e messaggi live pending.
6. Il browser aggiorna i messaggi live in-place mentre arrivano delta.
7. Quando lo stesso contenuto arriva da `state.db`, il messaggio live viene rimosso o marcato come riconciliato.

Hermes API server espone gia:

```text
POST /api/sessions/{session_id}/chat/stream
```

e produce eventi come:

```text
run.started
message.started
assistant.delta
tool.progress
tool.started
tool.completed
tool.failed
assistant.completed
run.completed
done
```

Per questa app bastano inizialmente:

| Evento | Uso nella live transcript |
| --- | --- |
| `run.started` | opzionale; conferma che il turno e iniziato |
| `message.started` | crea un placeholder assistant live |
| `assistant.delta` | concatena testo al placeholder |
| `assistant.completed` | marca il placeholder come completo ma ancora pending DB |
| `done` | chiude il worker stream |

### Stato in memoria in `server.py`

Struttura possibile:

```python
_live_events_by_session = {
    "20260629_065835_af2696": {
        "msg_abc": {
            "id": "live:msg_abc",
            "session_id": "20260629_065835_af2696",
            "role": "assistant",
            "display": "hermes",
            "content": "partial or complete text",
            "timestamp": 1782710123.45,
            "live": True,
            "completed": False,
            "source": "hermes_sse"
        }
    }
}
```

Note operative:

- usare un lock, perche `ThreadingHTTPServer` puo servire poll e worker stream in thread diversi
- limitare numero messaggi live per sessione
- applicare TTL, ad esempio 10-20 minuti
- eliminare sessioni live vecchie quando non sono piu la sessione pinnata

### Modifiche a `/api/current`

`_serve_current()` oggi:

1. legge messaggi Hermes da `state.db`
2. legge messaggi bus da `agent-bus-log.db`
3. fonde per timestamp
4. restituisce JSON al browser

Con live preview:

1. leggere DB e bus come oggi
2. leggere anche i live message per `sid`
3. filtrare i live gia riconciliati con il DB
4. fondere DB, bus e live per timestamp
5. includere campi extra sui live:

```json
{
  "id": "live:msg_abc",
  "display": "hermes",
  "role": "assistant",
  "content": "partial text",
  "timestamp": 1782710123.45,
  "live": true,
  "completed": false
}
```

Il campo `last_transcript_id` deve continuare a considerare solo ID numerici positivi del DB. I messaggi live non devono far avanzare il cursore DB.

### Riconciliazione live vs DB

Quando arriva un record canonico da `state.db`, il live placeholder va tolto per evitare duplicati.

Strategia V1:

- confrontare solo messaggi nella stessa `session_id`
- considerare solo stesso `role` (`assistant` o `user`)
- normalizzare spazi e trim
- se il live e completo, match esatto del contenuto normalizzato
- se il live e parziale, match quando il contenuto DB inizia con il contenuto live, o quando il live e prefisso significativo del DB
- dopo match, rimuovere il live dal buffer

Esempio:

```text
live: "Sto controllando i file"
db:   "Sto controllando i file e poi applico la patch."
```

puo essere considerato riconciliato se il live e stato marcato `completed=false` o e ancora recente. Per live `completed=true`, e preferibile richiedere match esatto o quasi-esatto per evitare cancellazioni sbagliate.

### Modifiche frontend

La UI attuale appende nuovi messaggi e rimuove i pending user message quando trova il corrispondente record `human`.

Per i live message serve update-in-place:

1. `buildMessageElement()` accetta ID stringa, non solo numerico.
2. ogni `.msg` riceve `data-message-key`.
3. `appendMessages()` diventa `upsertMessages()`:
   - se il key non esiste, crea il nodo
   - se esiste ed e live, aggiorna contenuto/stato
   - se arriva la versione DB, sostituisce o lascia che il live venga filtrato lato server
4. i live assistant hanno stile `pending` o un nuovo stato visivo leggero.

Chiave consigliata:

```javascript
function messageKey(msg) {
  if (msg.live) return msg.id;
  if (msg.is_bus) return `bus:${msg.bus_id}`;
  return `db:${msg.id}`;
}
```

### Modifiche a `/api/send`

Comportamento desiderato:

1. validare body come oggi
2. scegliere/creare sessione come oggi
3. aggiungere subito un pending user message locale, come gia fa il browser
4. avviare un thread background che:
   - chiama `/api/sessions/{session_id}/chat/stream`
   - legge righe SSE
   - aggiorna `_live_events_by_session`
   - chiude su `done` o errore
5. rispondere subito:

```json
{"ok": true, "queued": true, "streaming": true, "session_id": "..."}
```

In caso Hermes API server non supporti `/chat/stream`, fallback temporaneo:

- usare `/chat` come oggi
- nessun live assistant delta
- comportamento attuale preservato

### V2: supportare sessioni avviate fuori da questa UI

La V1 copre i turni inviati dalla sidebar di questo progetto. Non copre automaticamente:

- Hermes CLI
- gateway Telegram/Slack/Discord/etc.
- altri client che chiamano direttamente Hermes API server

Per quei casi serve un piccolo notifier lato Hermes.

Design V2:

1. Aggiungere in Hermes un helper fire-and-forget, per esempio:

```python
notify_live_transcript({
    "session_id": session_id,
    "event": "assistant.delta",
    "message_id": message_id,
    "delta": text,
    "role": "assistant",
    "timestamp": time.time(),
})
```

2. L'helper fa `POST http://127.0.0.1:8800/api/live-event`.
3. Timeout molto corto, ad esempio 100-300ms.
4. Mai propagare eccezioni: se live transcript non gira, Hermes non deve rallentare o fallire.
5. Wire iniziale nei callback gia esistenti:
   - `stream_delta_callback` per delta assistant
   - `interim_assistant_callback` per messaggi assistant intermedi
   - opzionalmente il punto in cui il user message viene aggiunto alla history

Endpoint da aggiungere in questo progetto:

```text
POST /api/live-event
```

Body indicativo:

```json
{
  "session_id": "20260629_065835_af2696",
  "message_id": "msg_abc",
  "event": "assistant.delta",
  "role": "assistant",
  "delta": "testo incrementale",
  "content": "snapshot completo opzionale",
  "timestamp": 1782710123.45
}
```

### Sicurezza

`server.py` ascolta solo su `127.0.0.1`, quindi il rischio e locale. Comunque:

- accettare solo JSON object
- limitare dimensione body, per esempio 64 KiB
- limitare dimensione contenuto live per messaggio
- scartare `session_id` vuoti o troppo lunghi
- opzionale: richiedere un token locale condiviso per `/api/live-event`

### Ordine di implementazione consigliato

1. Implementare buffer live in memoria e merge in `/api/current`.
2. Rendere il frontend capace di fare upsert dei messaggi.
3. Cambiare `/api/send` per consumare `/chat/stream`.
4. Aggiungere riconciliazione live/DB.
5. Aggiungere TTL e cleanup.
6. Solo dopo, valutare V2 con `POST /api/live-event` da Hermes.

### Criteri di successo

- dopo invio dalla sidebar, il messaggio assistant appare appena Hermes produce il primo delta
- il transcript continua a funzionare anche se lo stream cade
- quando `state.db` riceve il messaggio definitivo, non compaiono duplicati
- `last_transcript_id` resta basato solo sui record DB
- senza Hermes API server o senza `/chat/stream`, il comportamento degrada al polling attuale

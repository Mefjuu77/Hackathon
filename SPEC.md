# SPEC — Customer vs Help Desk Voicebot (MVP)

Specyfikacja do implementacji przez Claude Code. Źródła treści: `PROMPT_CUSTOMER.md`, `PROMPT_HELPDESK.md`, `PROPOZYCJA_EWALUACJA.md`. Ten dokument jest nadrzędny wobec `PROPOZYCJA_EWALUACJA.md` tam, gdzie się różnią.

Protokół ElevenLabs (EL) zweryfikowany z dokumentacją (AsyncAPI: https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket.md). Przy wątpliwościach co do pól — sprawdź tę stronę, nie zgaduj.

---

## 1. Cel i zakres

Tester ustawia poziom zadowolenia klienta (NPS 1–10), poziom obsługi konsultanta (CX 1–10) i scenariusz, klika Start i słyszy na żywo rozmowę głosową dwóch agentów EL (Customer ↔ Help Desk), widzi transkrypt i wykres nastroju klienta, a po zakończeniu pasek podsumowania.

**W zakresie (MVP):**
1. Konfiguracja dwóch agentów w panelu EL (ręcznie, wg checklisty) + skrypt weryfikujący.
2. Most audio agent ↔ agent przez dwie sesje WebSocket (backend).
3. Frontend: suwaki, wybór scenariusza, audio na żywo, transkrypt, replay zapisanych przebiegów.
4. `report_mood` + wykres, `end_call`, limit czasu, Stop, pasek podsumowania.

**Poza zakresem:** scorecard, LLM-judge, pobieranie analizy EL (evaluation criteria / data collection), case studies, tryb tekstowy / ewaluacja masowa, przycisk Pauza, server tools CRM, osobne voice ID per poziom, wiele rozmów równolegle, autoryzacja, baza danych, SDK `elevenlabs`.

---

## 2. Stack i konwencje

- Python 3.14, `uv`. Uruchomienie: `uv run uvicorn app.main:app --reload`.
- Zależności: `fastapi`, `uvicorn[standard]`, `websockets`, `httpx`, `python-dotenv`. Dev: `pytest`, `pytest-asyncio`.
- Audio wyłącznie stdlib (`array`, `wave`, `base64`). Uwaga: `audioop` nie istnieje w 3.13+.
- Frontend: czysty HTML/JS (bez frameworka i bundlera), Web Audio API, Chart.js z CDN.
- Identyfikatory i komentarze w kodzie po angielsku; wszystkie teksty UI po polsku.
- Klient EL na surowych `websockets` (bez SDK `elevenlabs`).
- Usuń szablonowy `main.py` z katalogu głównego.

### Struktura

```
app/
  main.py               # FastAPI: routing HTTP + WS, serwowanie static i runs
  config.py             # odczyt .env
  content.py            # persony, scenariusze, CRM, ustawienia głosu, budowanie init-data
  elevenlabs_session.py # klient jednej sesji EL (WS, ping/pong, parsowanie zdarzeń)
  audio.py              # bufor wyjściowy agenta, cisza, mikser stereo
  bridge.py             # orkiestracja przebiegu: 2 sesje, pętla tików, zakończenie
  run_manager.py        # blokada "jedna rozmowa naraz", start/stop
  run_store.py          # zapis plików przebiegu (wav, jsonl, json)
  replay.py             # odtwarzanie zapisanego przebiegu do UI
static/
  index.html
  app.js
scripts/
  check_agents.py       # weryfikacja konfiguracji agentów przez API EL
  run_cli.py            # przebieg z konsoli (bez UI)
tests/
  fake_elevenlabs.py    # fake serwer EL (WS) do testów integracyjnych
  test_*.py
runs/                   # wyniki przebiegów (w .gitignore)
.env.example
```

### `.env`

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `ELEVENLABS_API_KEY` | — | wymagane |
| `CUSTOMER_AGENT_ID` | — | wymagane |
| `HELPDESK_AGENT_ID` | — | wymagane |
| `ELEVENLABS_API_BASE` | `https://api.elevenlabs.io` | podmieniane w testach na fake serwer |
| `MAX_CONVERSATION_SECONDS` | `360` | twardy limit orchestratora |
| `EL_MAX_DURATION_SECONDS` | `420` | override `conversation.max_duration_seconds` (bezpiecznik po stronie EL) |
| `RUNS_DIR` | `runs` | katalog przebiegów |

`.env` i `runs/` w `.gitignore`. Klucz API nigdy nie trafia do przeglądarki.

---

## 3. Agenci w panelu ElevenLabs (krok ręczny — wykonuje człowiek)

Prompty i tabele: `PROMPT_CUSTOMER.md`, `PROMPT_HELPDESK.md`. Claude Code nie konfiguruje panelu — dostarcza checklistę (tę sekcję) i `scripts/check_agents.py`.

### Checklista (oba agenci, o ile nie zaznaczono inaczej)

| # | Ustawienie | Customer | Help Desk |
|---|---|---|---|
| A1 | Język | `pl` | `pl` |
| A2 | System prompt | z `PROMPT_CUSTOMER.md` | z `PROMPT_HELPDESK.md` |
| A3 | First message | `{{first_message}}` | puste |
| A4 | Głos | jeden męski głos klienta | jeden kobiecy głos (konsultantka „Joanna”) |
| A5 | `asr.user_input_audio_format` | `pcm_16000` | `pcm_16000` |
| A6 | `tts.agent_output_audio_format` | `pcm_16000` | `pcm_16000` |
| A7 | Systemowe narzędzie `end_call` | ✅ | ✅ |
| A8 | Client tool `report_mood` (`score` integer, `reason` string), `expects_response = false` | ✅ | — |
| A9 | Overrides włączone: `tts.stability`, `tts.speed`, `conversation.max_duration_seconds` | ✅ | ✅ |
| A10 | `platform_settings.auth.enable_auth = true` (agent prywatny) | ✅ | ✅ |
| A11 | `conversation.client_events` zawiera: `audio`, `agent_response`, `agent_response_correction`, `user_transcript`, `interruption`, `ping`, `client_tool_call`, `agent_tool_response` | ✅ | ✅ |
| A12 | `turn.turn_timeout = 10` | ✅ | ✅ |
| A13 | `turn.silence_end_call_timeout = -1` (wyłączony) | ✅ | ✅ |

Nie włączamy overrides promptu ani first message — wszystko zmienne idzie przez dynamic variables. Wysłanie overridu, który nie jest włączony, kończy się błędem EL, więc wysyłamy tylko A9.

### `scripts/check_agents.py`

- Dla każdego agenta: `GET {ELEVENLABS_API_BASE}/v1/convai/agents/{agent_id}`, nagłówek `xi-api-key`.
- Sprawdza A1, A3, A5–A7, A9–A13 na polach odpowiedzi:
  - `conversation_config.agent.language`, `conversation_config.agent.first_message`
  - `conversation_config.asr.user_input_audio_format`, `conversation_config.tts.agent_output_audio_format`
  - `conversation_config.agent.prompt.built_in_tools.end_call` (nie-null)
  - `platform_settings.overrides.conversation_config_override.tts.stability|speed`, `...conversation.max_duration_seconds` (true)
  - `platform_settings.auth.enable_auth`
  - `conversation_config.conversation.client_events` (nadzbiór wymaganej listy)
  - `conversation_config.turn.turn_timeout`, `conversation_config.turn.silence_end_call_timeout`
- A8: tool jest podpięty przez `conversation_config.agent.prompt.tool_ids`. Skrypt pobiera definicje narzędzi po ID (endpoint narzędzi — **zweryfikuj ścieżkę w dokumentacji API EL „Tools”**) i sprawdza, że istnieje narzędzie `type: "client"`, `name: "report_mood"` z parametrami `score`, `reason`. Jeśli endpointu nie da się potwierdzić — A8 raportowane jako `MANUAL` (nie FAIL).
- Wynik: tabela `PASS/FAIL/MANUAL` per agent i punkt; exit code 1, jeśli jakikolwiek FAIL.
- Logika sprawdzania to czysta funkcja `check_agent_config(role, config_json, tools) -> list[CheckResult]`, testowana na fixture JSON.

---

## 4. Treści — `app/content.py`

Jedno źródło prawdy w kodzie, przeniesione z plików MD (MD zostają jako dokumentacja; przy rozbieżności wygrywa `content.py`, ale treść ma być 1:1).

- `CUSTOMER_PERSONAS: dict[int, CustomerPersona]` — `persona` (tekst), `patience_turns` (int) dla poziomów 1–10.
- `HELPDESK_PERSONAS: dict[int, str]` — opis poziomu 1–10.
- `SCENARIOS: dict[str, Scenario]` — `S1`..`S4`. Pola: `id`, `title` (np. „Technical — brak internetu”), `customer_name`, `scenario`, `hidden_facts: list[str]`, `first_messages: {low, mid, high}` (NPS 1–3 / 4–7 / 8–10), `crm_data`, `expected_resolution` (**nigdy nie trafia do agentów**).
- Klient jest zawsze mężczyzną: S2 = Andrzej Kowalski, S4 = Krzysztof Zieliński (już poprawione w MD).
- `AGENT_NAME = "Joanna"`.
- Ustawienia głosu (override TTS — tylko `stability` i `speed`; `style` nie istnieje w API):

| Poziom | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| Customer `stability` | 0.25 | 0.35 | 0.40 | 0.40 | 0.50 | 0.55 | 0.50 | 0.50 | 0.45 | 0.45 |
| Customer `speed` | 1.15 | 1.10 | 1.05 | 1.05 | 1.00 | 1.00 | 1.00 | 0.95 | 0.95 | 0.95 |
| Help Desk `stability` | 0.60 | 0.60 | 0.60 | 0.60 | 0.55 | 0.55 | 0.50 | 0.50 | 0.45 | 0.45 |
| Help Desk `speed` | 1.10 | 1.10 | 1.05 | 1.05 | 1.00 | 1.00 | 1.00 | 0.95 | 0.95 | 0.95 |

### Budowanie danych startowych

`build_customer_init(nps_level, scenario_id, el_max_duration) -> dict` i `build_helpdesk_init(cx_level, scenario_id, el_max_duration) -> dict` zwracają gotową wiadomość:

```json
{
  "type": "conversation_initiation_client_data",
  "dynamic_variables": { "...": "..." },
  "conversation_config_override": {
    "tts": { "stability": 0.35, "speed": 1.10 },
    "conversation": { "max_duration_seconds": 420 }
  }
}
```

- Customer `dynamic_variables`: `nps_level`, `persona`, `patience_turns`, `scenario`, `hidden_facts`, `customer_name`, `first_message`.
- Help Desk `dynamic_variables`: `cx_level`, `persona`, `agent_name`, `crm_data`.
- Wszystkie wartości jako stringi. `hidden_facts` jako linie `- fakt\n`.
- Zawsze komplet zmiennych (zachowanie EL przy brakującej zmiennej nie jest udokumentowane).
- `pick_scenario("random")` losuje z S1–S4.

---

## 5. Protokół ElevenLabs (to, czego używamy)

**Połączenie:** `GET {API_BASE}/v1/convai/conversation/get-signed-url?agent_id=...`, nagłówek `xi-api-key` → `{"signed_url": "wss://..."}`. URL traktuj jako nieprzezroczysty, użyj w ciągu 15 min. Każda sesja ma własny signed URL.

**Wysyłamy:**
- `conversation_initiation_client_data` (sekcja 4) — pierwsza wiadomość po połączeniu.
- `{"user_audio_chunk": "<base64 PCM16 LE mono 16 kHz>"}` — bez pola `type`.
- `{"type": "pong", "event_id": <ping_event.event_id>}` — na każdy `ping`, natychmiast.
- `{"type": "client_tool_result", "tool_call_id": "...", "result": "ok", "is_error": false}` — na każdy `client_tool_call`, także gdy `expects_response` = false.

**Odbieramy (pola zagnieżdżone w obiekcie `<nazwa>_event` lub podobnym — patrz AsyncAPI):**
- `conversation_initiation_metadata` → `conversation_initiation_metadata_event.{conversation_id, agent_output_audio_format, user_input_audio_format}`. Oba formaty muszą być `pcm_16000`, inaczej błąd przebiegu.
- `audio` → `audio_event.{audio_base_64, event_id, is_final}`.
- `agent_response` → `agent_response_event.{agent_response, event_id}` (przychodzi zwykle po rozpoczęciu strumienia audio tej wypowiedzi).
- `agent_response_correction` → `agent_response_correction_event.{original_agent_response, corrected_agent_response, event_id}`.
- `user_transcript` → `user_transcription_event.{user_transcript, event_id}` (tylko do logu).
- `interruption` → `interruption_event.{event_id}`.
- `ping` → `ping_event.{event_id, ping_ms}`.
- `client_tool_call` → `client_tool_call.{tool_name, tool_call_id, parameters, event_id, expects_response}`.
- `agent_tool_response` → `agent_tool_response.{tool_name, tool_type, ...}`; `tool_name == "end_call"` = agent zakończył rozmowę.
- `client_error` → błąd przebiegu.
- Nieznane typy: zapis do logu, bez błędu.

Nie ma dedykowanego zdarzenia końca rozmowy — koniec = `agent_tool_response` z `end_call` **albo** zamknięcie socketu przez serwer.

---

## 6. Backend

### 6.1 `ElevenLabsSession` (`elevenlabs_session.py`)

Jedna sesja jednego agenta (`role`: `"customer"` | `"helpdesk"`).

- `connect(init_message)`: pobiera signed URL (httpx), otwiera WS, wysyła init, czeka na `conversation_initiation_metadata` (timeout 15 s → błąd), waliduje formaty audio.
- Pętla odbioru (task asyncio): `ping` → pong od razu (obsługiwane wewnątrz sesji); pozostałe zdarzenia → callback / kolejka do `Bridge` jako znormalizowane obiekty (`AudioChunk(pcm: bytes, is_final)`, `AgentResponse(text)`, `Correction(original, corrected)`, `Interruption`, `ToolCall(name, call_id, params)`, `AgentToolResponse(name)`, `Closed(code, reason)`, `ErrorEvent(...)`).
- Każde surowe zdarzenie trafia do `events.jsonl` (z `audio_base_64` zastąpionym długością w bajtach).
- `send_audio(pcm: bytes)`, `send_tool_result(call_id)`, `close()`.

### 6.2 Audio (`audio.py`)

- Format wszędzie: PCM16 LE, mono, 16 kHz. Tik = 100 ms = 1600 próbek = **3200 bajtów**.
- `OutputBuffer` per agent (audio wygenerowane przez agenta, czekające na przekazanie): `append(pcm)`, `take(n) -> bytes` (zwraca do `n` bajtów, dopełnia zerami do `n`), `clear()`, `is_empty`.
- `mix_stereo(left: bytes, right: bytes) -> bytes` — przeplot próbek (L = klient, P = konsultant), wynik 6400 bajtów na tik.

### 6.3 `Bridge` (`bridge.py`) — jeden przebieg

1. Tworzy katalog przebiegu (`RunStore`), otwiera obie sesje **równolegle** (`asyncio.gather`), każda z własnym init.
2. Audio, które przyjdzie przed startem pętli (np. first message klienta), trafia do bufora — nie ginie.
3. Gdy **obie** sesje potwierdzą start → emituje `run_started` i rusza pętla tików.
4. **Pętla tików** (jedna dla obu kierunków, harmonogram po `time.monotonic()`: `next_tick += 0.1`, bez dryfu):
   - `c = customer_buf.take(3200)`, `h = helpdesk_buf.take(3200)`;
   - `helpdesk.send_audio(c)`, `customer.send_audio(h)` — zawsze, także gdy to cisza;
   - `stereo = mix_stereo(c, h)` → binarna ramka do przeglądarki + dopisanie do `audio.wav`.
5. **Przerwanie:** `interruption` z sesji X → `X_buf.clear()`, licznik przerwań X += 1, bieżąca wypowiedź X oznaczona `interrupted: true` w transkrypcie, zdarzenie `interruption` do UI.
6. **Transkrypt:** `agent_response` z sesji X → nowa wypowiedź (`utterance_id`, `role`, `text`, `t` od startu). `agent_response_correction` → podmiana tekstu ostatniej pasującej wypowiedzi X + zdarzenie `transcript_correction`. `user_transcript` tylko do logu.
7. **Tury:** jedna tura = jedna wypowiedź agenta (`agent_response`). Liczone osobno dla obu ról.
8. **`report_mood`** (`client_tool_call` od klienta z `tool_name == "report_mood"`): odpowiedz `client_tool_result`; `score` rzutuj na int i przytnij do 1–10 (niepoprawny → zignoruj punkt, zaloguj); punkt `{turn: liczba dotychczasowych wypowiedzi konsultanta, score, reason}` → zdarzenie `mood` do UI. Inne client tools → `client_tool_result` z `is_error: true`.
9. **Zakończenie** (pierwsze wygrywa, `end_reason`):
   - `customer_end_call` / `helpdesk_end_call` — `agent_tool_response` z `end_call`;
   - `customer_closed` / `helpdesk_closed` — serwer zamknął socket bez `end_call`;
   - `timeout` — `MAX_CONVERSATION_SECONDS` od startu pętli;
   - `stopped` — Stop z UI;
   - `error` — błąd połączenia, `client_error`, zła walidacja formatu, timeout startu, wyjątek.

   Dla `*_end_call` i `*_closed`: pętla działa dalej, aż oba bufory będą puste (maks. 5 s), potem jeszcze 1 s ciszy, potem zamknięcie obu sesji. Dla `timeout`, `stopped`, `error`: zamknięcie natychmiast. Bez ponownych prób połączenia.
10. Na końcu (zawsze, także przy `error`): domknięcie WAV, zapis `transcript.json`, `summary.json`, zdarzenie `run_ended` do UI.

`Bridge` nie zależy od FastAPI: przyjmuje `emit(event_dict)` i `emit_audio(bytes)` jako callbacki — dzięki temu `run_cli.py` i testy używają go bez serwera.

### 6.4 `RunManager`

- Co najwyżej jeden aktywny przebieg. `start(...)` w trakcie innego → zdarzenie `error` z komunikatem „Rozmowa już trwa”.
- `stop()` → `Bridge` kończy z `stopped`.
- Rozłączenie przeglądarki w trakcie → przebieg kończy się `stopped`.

### 6.5 Pliki przebiegu (`RunStore`)

Katalog `runs/<YYYYmmdd-HHMMSS>_<scenario_id>/` (`run_id` = nazwa katalogu):

- `audio.wav` — stereo, 16 bit, 16 kHz, zapisywany przyrostowo.
- `events.jsonl` — każda linia: `{"t": <s od startu>, "source": "customer"|"helpdesk"|"ui"|"bridge", "event": {...}}`. `source: "ui"` = dokładnie to, co poszło do przeglądarki (JSON); na tym opiera się replay.
- `transcript.json` — `[{"utterance_id", "role", "text", "t", "interrupted"}]`.
- `summary.json` — patrz 6.6.

### 6.6 Podsumowanie (`summary.json` i `run_ended.summary`)

```json
{
  "run_id": "20261125-143000_S1",
  "scenario_id": "S1", "nps_level": 3, "cx_level": 9,
  "end_reason": "customer_end_call",
  "error": null,
  "duration_s": 182.4,
  "turns": {"customer": 8, "helpdesk": 7},
  "interruptions": {"customer": 1, "helpdesk": 2},
  "mood": {"start": 3, "end": 8, "delta": 5, "points": [{"turn": 0, "score": 3, "reason": "start"}]},
  "conversation_ids": {"customer": "...", "helpdesk": "..."}
}
```

`mood.points` zaczyna się od `(0, nps_level)`. `end` = ostatni punkt.

### 6.7 HTTP / WS (`main.py`)

| Endpoint | Opis |
|---|---|
| `GET /` | `static/index.html` |
| `GET /static/*` | pliki frontendu |
| `GET /api/scenarios` | `[{"id": "S1", "title": "..."}]` |
| `WS /ws` | rozmowa na żywo (protokół 6.8) |
| `GET /replay/{run_id}` | `index.html` w trybie replay |
| `WS /ws/replay/{run_id}` | odtworzenie przebiegu (protokół 6.8, bez komend) |
| `GET /runs/{run_id}/{file}` | pliki przebiegu (tylko 4 nazwy z 6.5; ochrona przed path traversal) |

### 6.8 Protokół przeglądarka ↔ backend

**Przeglądarka → backend (JSON):**
- `{"type": "start", "nps_level": 1-10, "cx_level": 1-10, "scenario_id": "S1".."S4" | "random"}`
- `{"type": "stop"}`

**Backend → przeglądarka:**
- **Ramki binarne:** 100 ms audio stereo PCM16 LE 16 kHz (6400 bajtów), L = klient, P = konsultant.
- **Ramki tekstowe (JSON):**
  - `run_started` `{run_id, scenario_id, scenario_title, nps_level, cx_level, customer_name, agent_name}`
  - `transcript` `{utterance_id, role, text, t}`
  - `transcript_correction` `{utterance_id, role, text}`
  - `interruption` `{role}`
  - `mood` `{turn, score, reason}`
  - `run_ended` `{end_reason, summary, files: {audio, transcript}}` (URL-e do `/runs/...`)
  - `error` `{message}`

**Replay:** backend czyta `events.jsonl` (tylko `source: "ui"`) i `audio.wav`; wysyła zdarzenia w oryginalnych odstępach czasu, a audio w ramkach 100 ms w tempie real-time — UI nie odróżnia replayu od rozmowy na żywo. Nie wymaga klucza EL.

---

## 7. Frontend (`static/`)

Układ wg makiety w `PROPOZYCJA_EWALUACJA.md` §5, bez przycisku Pauza i bez sekcji scorecard.

- **Sterowanie:** suwak „Klient NPS” 1–10 (domyślnie 3), suwak „Konsultant CX” 1–10 (domyślnie 8), dropdown scenariusza (z `/api/scenarios` + „Losuj”), przyciski „▶ Start rozmowy” i „⏹ Stop”. Podczas przebiegu suwaki, dropdown i Start są zablokowane; Stop aktywny tylko w trakcie.
- **Audio:** `AudioContext` tworzony przy kliknięciu Start (wymóg gestu). Ramki binarne → `AudioBuffer` 2 kanały 16 kHz (Int16 → Float32), kolejkowane `AudioBufferSourceNode.start(t)` z wyprzedzeniem ~200 ms; przy niedoborze bufora zaczyna od `currentTime + 0.2`.
- **Transkrypt na żywo:** „👤 Klient: …” / „🎧 Konsultant: …”, autoscroll, korekty podmieniają tekst, przerwana wypowiedź oznaczona (np. „⤳ przerwano”).
- **Nastrój:** Chart.js, linia z punktami, oś X „wypowiedź konsultanta”, oś Y 1–10, tooltip z `reason`; pierwszy punkt `(0, nps_level)` z `run_started`.
- **Pasek podsumowania** (po `run_ended`): przyczyna końca (po polsku, np. „Klient zakończył rozmowę”, „Limit czasu”, „Zatrzymano”, „Błąd: …”), czas trwania, tury klient/konsultant, przerwania, nastrój „3 → 8 (+5)”, linki „⬇ audio.wav” i „⬇ transcript.json”.
- **Błędy:** zdarzenie `error` → czerwony komunikat nad transkryptem.
- **Replay:** gdy ścieżka to `/replay/{run_id}`, strona ukrywa sterowanie, łączy się z `/ws/replay/{run_id}` po kliknięciu „▶ Odtwórz” (gest dla audio).

---

## 8. Testy

- `pytest` + `pytest-asyncio`. Wszystkie testy automatyczne działają **bez** klucza EL i bez sieci.
- **`tests/fake_elevenlabs.py`** — lokalny serwer (HTTP signed URL + WS) uruchamiany w teście, z konfigurowalnym skryptem per rola:
  - odpowiada na `get-signed-url` URL-em do siebie,
  - po init wysyła `conversation_initiation_metadata` (format konfigurowalny, by testować niezgodność),
  - wysyła audio szybciej niż real-time (burst), z `is_final` na końcu wypowiedzi, oraz `agent_response`,
  - wysyła `ping` i sprawdza, czy przyszedł `pong` z tym samym `event_id`,
  - potrafi wysłać `interruption`, `agent_response_correction`, `client_tool_call(report_mood)`, `agent_tool_response(end_call)`, `client_error`, oraz zamknąć socket,
  - nagrywa wszystkie odebrane wiadomości (do asercji).
- Testy jednostkowe: `OutputBuffer`, `mix_stereo`, harmonogram tików (bez dryfu), `content` (budowanie init, komplet zmiennych, wybór first message po NPS, wartości głosu, brak `expected_resolution` w danych dla agentów), `check_agent_config`, liczenie podsumowania, przycinanie `score`.
- Ręczny **smoke test** z prawdziwym EL — opisany w E2 i E4; nie jest częścią `pytest`.

---

## 9. Etapy i kryteria akceptacji

Każdy etap kończy się zielonym `uv run pytest`. Kryteria oznaczone 🧑 wymagają człowieka (prawdziwe EL / słuchanie).

### E0 — Szkielet
- [ ] `pyproject.toml` z zależnościami z §2; `uv sync` przechodzi.
- [ ] Struktura katalogów z §2; usunięty szablonowy `main.py`.
- [ ] `.env.example` z wszystkimi zmiennymi z §2; `.gitignore` zawiera `.env`, `runs/`, `.venv/`.
- [ ] `uv run uvicorn app.main:app` startuje; `GET /` zwraca stronę; `GET /api/scenarios` zwraca 4 scenariusze (po E1).
- [ ] `uv run pytest` przechodzi (min. 1 test dymny aplikacji przez `TestClient`).

### E1 — Treści i weryfikacja agentów
- [ ] `app/content.py` zawiera 10 person klienta (z `patience_turns`), 10 person konsultanta, 4 scenariusze (z 3 wariantami first message i `crm_data`) i tabelę głosu z §4 — treść zgodna 1:1 z plikami MD.
- [ ] Test: dla każdej kombinacji NPS 1–10 × CX 1–10 × S1–S4 init zawiera komplet zmiennych z §4, wszystkie wartości to niepuste stringi, overrides mają tylko `tts.stability`, `tts.speed`, `conversation.max_duration_seconds`.
- [ ] Test: first message to wariant `low` dla NPS 1–3, `mid` dla 4–7, `high` dla 8–10.
- [ ] Test: żaden tekst wysyłany do agentów nie zawiera `expected_resolution`.
- [ ] Test: `speed` w zakresie 0.7–1.2, `stability` w 0–1.
- [ ] `scripts/check_agents.py` zaimplementowany; testy `check_agent_config` na fixture'ach: poprawna konfiguracja → same PASS; każde z naruszeń A1, A3, A5–A7, A9–A13 → odpowiedni FAIL.
- [ ] 🧑 Agenci skonfigurowani wg checklisty §3; `uv run python scripts/check_agents.py` → brak FAIL.

### E2 — Most audio (konsola)
- [ ] `ElevenLabsSession`, `audio.py`, `Bridge`, `RunStore` wg §6.1–6.3, 6.5.
- [ ] `uv run python scripts/run_cli.py --nps 3 --cx 9 --scenario S1` prowadzi przebieg bez UI, wypisuje transkrypt na konsolę i zapisuje katalog w `runs/`.
- [ ] Testy integracyjne z fake serwerem:
  - [ ] oba agenty dostają init z oczekiwanymi zmiennymi; pętla rusza dopiero po metadata obu sesji;
  - [ ] audio wysłane burstem przez A dociera do B w tempie real-time (±20%: np. 2 s audio ≈ 2 s wysyłania), w ramkach po 3200 bajtów;
  - [ ] gdy A nic nie mówi, B dostaje ramki ciszy (zera) co ~100 ms;
  - [ ] `ping` → `pong` z tym samym `event_id`;
  - [ ] `interruption` od A czyści bufor A (B przestaje dostawać audio A w kolejnym tiku) i zwiększa licznik;
  - [ ] niezgodny format audio w metadata → `end_reason = error`;
  - [ ] `audio.wav` jest stereo 16 kHz, długość ≈ czas trwania pętli; kanał L = audio klienta, P = konsultanta;
  - [ ] `events.jsonl`, `transcript.json`, `summary.json` istnieją i mają format z §6.5–6.6; `audio_base_64` nie jest zapisywane w logu.
- [ ] 🧑 Smoke test: prawdziwa rozmowa S1 (NPS 3, CX 9) z `run_cli.py` — `audio.wav` brzmi jak naturalna rozmowa: klient zaczyna, tury się zmieniają, agenci nie mówią „halo, jest Pan tam?” w normalnym toku rozmowy. **Ryzyko do potwierdzenia tutaj:** dokumentacja EL nie potwierdza, że ramki ciszy są potrzebne do wykrycia końca tury — jeśli tury się nie zmieniają lub są opóźnione, zgłoś to przed E3.

### E3 — Frontend i replay
- [ ] Endpointy z §6.7 i protokół §6.8; `RunManager` wg §6.4.
- [ ] Frontend wg §7 (bez wykresu i paska podsumowania — to E4).
- [ ] Test (`TestClient` + fake serwer): `start` po WS → przychodzą `run_started`, ramki binarne 6400 B, `transcript`; drugi `start` w trakcie → `error` „Rozmowa już trwa”; `stop` → `run_ended` z `stopped`.
- [ ] Test: `/runs/{run_id}/../x` i nieznane nazwy plików → 404.
- [ ] Test: replay zapisanego przebiegu odtwarza te same zdarzenia `ui` w tej samej kolejności i audio z `audio.wav`.
- [ ] 🧑 W przeglądarce: słychać rozmowę na żywo (klient w lewym, konsultant w prawym kanale), transkrypt pojawia się na bieżąco, Stop przerywa rozmowę; `/replay/<run_id>` odtwarza ją bez klucza EL.

### E4 — Nastrój, zakończenie, podsumowanie
- [ ] `report_mood` wg §6.3 pkt 8; wykres nastroju wg §7.
- [ ] Zakończenie wg §6.3 pkt 9, `summary.json` i pasek podsumowania wg §6.6 i §7.
- [ ] Testy z fake serwerem:
  - [ ] `client_tool_call(report_mood)` → `client_tool_result` z tym samym `tool_call_id`, `result: "ok"`, `is_error: false`; zdarzenie `mood` z `turn` = liczba wypowiedzi konsultanta; `score` 15 → 10, `score` 0 → 1, `score` „abc” → brak punktu;
  - [ ] `agent_tool_response(end_call)` od klienta → pozostałe audio w buforach zostaje dograne, potem ~1 s, obie sesje zamknięte, `end_reason = customer_end_call`;
  - [ ] serwer zamyka socket konsultanta bez `end_call` → `helpdesk_closed`;
  - [ ] przekroczenie `MAX_CONVERSATION_SECONDS` (w teście np. 2 s) → `timeout`;
  - [ ] `client_error` lub nieudane połączenie jednej sesji → druga zamknięta, `error` z komunikatem, pliki zapisane;
  - [ ] `summary.json`: poprawne tury, przerwania, `mood.start/end/delta`, `duration_s`.
- [ ] 🧑 Smoke test w przeglądarce: S1 NPS 2 / CX 9 i S4 NPS 8 / CX 2 — wykres nastroju rośnie / spada zgodnie z oczekiwaniem, rozmowa kończy się sama (`end_call`) przed limitem czasu, pasek podsumowania i linki do pobrania działają.

---

## 10. Znane ryzyka

- **Ramki ciszy a VAD** — założenie niepotwierdzone w dokumentacji (sprawdzane w smoke teście E2).
- **`turn_timeout`** — jeśli drugi agent odpowiada dłużej niż 10 s, słuchający może się „odezwać”; wtedy podnieść w panelu.
- **Klient nie wywołuje `report_mood` po każdej wypowiedzi** — wykres pozostaje płaski między wywołaniami; poprawa promptu, nie kodu.
- **Zamknięcie socketu po `end_call`** — moment i kod zamknięcia nie są udokumentowane; obsługujemy oba sygnały (§5).
- **Konto EL w kolejce (limit współbieżności)** — nie obsługujemy specjalnie; ewentualne zamknięcie sesji = `error`.

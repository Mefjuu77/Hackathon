# Customer vs. Call Center Voicebot — propozycja rozwiązania

> Zakres MVP: **tylko tryb pełnego głosu** (agent ↔ agent przez audio). Ewaluacja masowa (batch tekstowy) odłożona — patrz sekcja 8.
>
> Uwaga: nazwy pól i endpointów ElevenLabs sprawdźcie w dokumentacji: https://elevenlabs.io/docs/eleven-agents/overview

---

## 1. Architektura

```
                 ┌──────────────── Backend (FastAPI + asyncio) ────────────────┐
 Przeglądarka    │                                                              │
 (suwaki, audio, │   Customer WS (EL) ──audio──► [orchestrator] ──audio──► HelpDesk WS (EL)
  transkrypt,  ◄─┼──── WS zdarzeń       ▲          │  tempo real-time         │
  wykres)        │                       └──audio───┤  cisza między turami ◄───┘
                 │                                  │  miks stereo → przeglądarka
                 │   client tool: report_mood()     │  server tools: CRM mock
                 │                                  ▼
                 │          po rozmowie: EL Analysis + LLM-judge → scorecard
                 └──────────────────────────────────────────────────────────────┘
```

- Dwóch agentów w ElevenLabs Agents: **Customer** i **Help Desk**.
- Backend otwiera dla każdego osobną sesję WebSocket i przekazuje audio jednego jako wejście drugiego.
- Przeglądarka dostaje miks audio (L = klient, P = agent), transkrypt i nastrój na żywo.

### Panel ElevenLabs vs API
| | Panel ElevenLabs | API (nasz kod) |
|---|---|---|
| Rozmowa człowiek ↔ agent | ✅ „Test AI agent”, można wpisać zmienne dynamiczne | ✅ |
| Rozmowa agent ↔ agent | ❌ | ✅ orchestrator |
| Edycja promptów, głosu, narzędzi, kryteriów ewaluacji | ✅ | ✅ |
| Historia rozmów, transkrypt, audio, wyniki analizy | ✅ | ✅ pobranie po ID rozmowy |

Panel służy do strojenia każdego agenta osobno (np. sami gramy klienta przeciwko Help Deskowi z CX 2).

---

## 2. Komunikacja audio przez WebSocket

Wiadomości JSON z audio w base64.

**Klient → ElevenLabs**
```json
{"type": "conversation_initiation_client_data", "dynamic_variables": {...}, "conversation_config_override": {...}}
{"user_audio_chunk": "<base64 PCM16, 16 kHz, mono>"}
{"type": "pong", "event_id": 123}
{"type": "client_tool_result", ...}
```

**ElevenLabs → klient**
```json
{"type": "audio", "audio_event": {"audio_base_64": "...", "event_id": 7}}
{"type": "agent_response", ...}
{"type": "user_transcript", ...}
{"type": "interruption", ...}
{"type": "client_tool_call", ...}
{"type": "ping", ...}
```

### Pułapki mostu audio
1. **Tempo.** TTS generuje szybciej niż real-time. Chunki trzeba przekazywać w tempie odtwarzania (100 ms audio co 100 ms).
2. **Cisza.** Gdy agent A skończy mówić, dalej wysyłamy do B ramki ciszy. Dopiero wtedy VAD agenta B wykryje koniec tury.
3. **Format.** U obu agentów ten sam format (np. PCM 16 kHz na wejściu i wyjściu), żeby uniknąć resamplingu.
4. **Ping/pong.** Trzeba odpowiadać na `ping`, inaczej sesja zostanie zamknięta.
5. **Kto zaczyna.** Klient zaczyna rozmowę (first message), Help Desk czeka albo tylko się wita.
6. **Koniec rozmowy.** Narzędzie systemowe `end_call` u obu agentów + twardy limit czasu w orchestratorze.

Zaleta: przerwania dzieją się naturalnie. Jeśli B zacznie mówić, gdy A jeszcze nadaje, A dostaje `interruption`.

```python
async def pump(src_queue, dst_ws, listener):
    while True:
        chunk = await src_queue.get_or_silence(timeout=0.1)  # 100 ms audio albo cisza
        await dst_ws.send(json.dumps({"user_audio_chunk": b64(chunk)}))
        await listener.send(chunk)                            # do przeglądarki
        await asyncio.sleep(0.1)                              # tempo real-time
```

---

## 3. Sterowanie poziomami (1–10)

### a) Jeden agent na rolę i zmienne dynamiczne
Dwóch agentów z promptami sparametryzowanymi przez dynamic variables: `{{nps_level}}`, `{{cx_level}}`, `{{scenario}}`, `{{hidden_facts}}`. Overrides na starcie rozmowy podmieniają prompt, first message i głos (trzeba je włączyć w ustawieniach bezpieczeństwa agenta).

### b) Poziom rozbity na mierzalne „pokrętła”

| Parametr (Customer) | NPS 1 | NPS 5 | NPS 10 |
|---|---|---|---|
| Cierpliwość (ile tur bez postępu przed eskalacją) | 1–2 | 4 | 8+ |
| Ton | sarkazm, przerywanie, groźba odejścia | rzeczowy | ciepły, dziękuje |
| Gotowość do wykonywania instrukcji | odmawia / „już to robiłem” | robi | robi chętnie |
| Ile informacji podaje sam z siebie | minimum | odpowiada na pytania | dużo, z kontekstem |
| Reakcja na generyczną odpowiedź | eskaluje, żąda przełożonego | dopytuje | akceptuje |

| Parametr (Help Desk) | CX 1 | CX 5 | CX 10 |
|---|---|---|---|
| Empatia | brak, zbywa | formalna | aktywne słuchanie, parafraza |
| Diagnoza | 0–1 pytanie, skrypt | standardowy proces | hipotezy, celowane pytania, używa narzędzi CRM |
| Reakcja na złość | odbija, bywa niegrzeczny | neutralna | deeskalacja (uznanie emocji, przeprosiny, konkretny plan) |
| Domknięcie | brak | podsumowanie | podsumowanie, następne kroki, proaktywna oferta |

Prompt dostaje kotwice dla poziomów 1, 3, 5, 7 i 10 oraz instrukcję interpolacji, plus 1–2 przykładowe wypowiedzi na kotwicę (few-shot).

### c) Głos zależny od poziomu
- **Voice settings:** niższe `stability` = bardziej emocjonalny głos (niskie NPS); `speed` (zdenerwowany klient mówi szybciej); `style`.
- **Inne voice ID** dla skrajnych poziomów, jeśli różnica jest za mała.
- **Tagi ekspresji audio** w modelach v3 (np. `[sighs]`, `[frustrated]`).
- **Turn-taking:** krótszy timeout i skłonność do przerywania dla zniecierpliwionego klienta.

Ważne: drugi bot nie „słyszy” tonu — ASR przekazuje LLM-owi tylko słowa. Emocje klienta muszą więc być widoczne także w treści wypowiedzi, nie tylko w głosie.

### d) Ukryty, zmienny stan klienta
- Startowy nastrój `mood = nps_level`. Po każdej turze klient wywołuje **client tool** `report_mood(score, reason)` → wykres satysfakcji na żywo.
- Reguła: dobra obsługa +1/+2, zła −1/−2, mnożnik zależny od osobowości.
- Na koniec ankieta w roli: „W skali 0–10, jak polecił(a)by Pan(i)…?”.

### e) Ukryta prawda scenariusza (ground truth)
Każdy scenariusz ma fakty znane tylko klientowi (np. „awaria po zmianie taryfy, router już restartowany”). Help Desk ma mockowe **server tools** (`check_line_status`, `get_invoice`, `create_ticket`, `get_ticket_history`). Agent z wysokim CX ich używa, z niskim pomija.

---

## 4. Ewaluacja pojedynczej rozmowy

### Wbudowana analiza ElevenLabs
- **Evaluation criteria** (success/failure/unknown z uzasadnieniem): „problem rozwiązany”, „agent okazał empatię”, „co najmniej 2 pytania diagnostyczne”, „klient nie zażądał przełożonego”.
- **Data collection:** `final_nps`, `issue_category`, `resolved`, `escalated`, `root_cause_identified`.
- Pobieramy przez API conversations (po ID rozmowy) albo post-call webhook.

### LLM-judge (Claude) na transkrypcie
```json
{
  "resolution": {"score": 4, "resolved": true},
  "diagnosis_quality": {"score": 3, "facts_uncovered": ["router_restarted"], "facts_missed": ["tariff_change"]},
  "empathy": 5, "clarity": 4, "deescalation": 4, "professionalism": 5,
  "customer_persona_estimated_nps": 2,
  "agent_persona_estimated_cx": 9,
  "key_moments": [{"turn": 6, "event": "customer escalated", "cause": "generic answer"}]
}
```
**Wierność persony:** judge nie zna ustawionych poziomów i sam je szacuje. Rozbieżność = sygnał do poprawy promptów.

### Metryki liczone kodem
- Liczba tur, czas trwania, liczba przerwań (`interruption`), udział mówienia.
- ΔNPS i trajektoria nastroju z `report_mood`.
- Odsetek odkrytych ukrytych faktów, wywołania narzędzi, czy utworzono ticket.

---

## 5. UI dla testera

```
┌─────────────────────────────────────────────────────────────┐
│  Customer vs Help Desk                                       │
│  Customer NPS [===●-------] 3      Help Desk CX [========●-] 9 │
│  Problem      [ Losuj ▾ ]                [ ▶ Start rozmowy ]  │
├──────────────────────────────┬──────────────────────────────┤
│ 🔊 Transkrypt na żywo         │ Nastrój klienta               │
│ 👤 Klient: Trzeci raz dzwonię!│ 10 ┤                    ╭──   │
│ 🎧 Agent: Rozumiem frustrację,│  5 ┤        ╭───────────╯     │
│    sprawdzę to od razu...     │  1 ┤────────╯                 │
│            [⏸ Pauza] [⏹ Stop] │     tura 1  3  5  7  9        │
├──────────────────────────────┴──────────────────────────────┤
│ Scorecard: Rozwiązano ✅  ΔNPS +5 (3→8)  Tur 14  Przerwań 3   │
│ Empatia 5/5 · Diagnoza 4/5 · Deeskalacja 5/5                 │
│ Persona: NPS 3 / oszac. 3 · CX 9 / oszac. 8                  │
│ [⬇ Pobierz transkrypt + audio]                               │
└─────────────────────────────────────────────────────────────┘
```

Przebieg:
1. Tester ustawia suwaki i problem, klika Start.
2. Backend otwiera dwie sesje WS z dynamic variables i overrides.
3. Tester słyszy rozmowę (stereo), widzi transkrypt i wykres nastroju.
4. Po zakończeniu: analiza ElevenLabs obu rozmów + LLM-judge → scorecard.

---

## 6. Stack
- **Backend:** FastAPI, asyncio, `websockets`; jeden WS do przeglądarki ze zdarzeniami (audio, tekst, nastrój).
- **Frontend:** prosta strona HTML/JS z Web Audio API (bez frameworka na start).
- **ElevenLabs:** 2 agenci, PCM 16 kHz, overrides włączone, `end_call`, `report_mood`, CRM mock jako server tools.
- **Judge:** Claude przez API.

---

## 7. Kolejność prac
1. Dwóch agentów w panelu ElevenLabs: prompty z kotwicami poziomów, głosy, zmienne dynamiczne. Testy ręczne w panelu.
2. Orchestrator: dwie sesje WS, most audio (tempo, cisza, ping/pong), zapis do WAV. Najpierw z konsoli.
3. Frontend: suwaki, odtwarzanie audio, transkrypt na żywo.
4. `report_mood` + wykres nastroju, `end_call`, limit czasu.
5. Scorecard: analiza ElevenLabs + LLM-judge.
6. Przygotowanie dwóch case studies: NPS 2 + CX 9 oraz NPS 8 + CX 2.

---

## 8. Później (jeśli starczy czasu)
- **Ewaluacja masowa w moście tekstowym:** siatka NPS × CX × scenariusze × seedy, heatmapy ΔNPS i resolution rate, testy monotoniczności.
- **Walidacja tekst vs głos:** te same kombinacje w obu trybach, porównanie metryk.
- **Realizm trybu tekstowego:** sztuczne błędy ASR, symulowane przerwania.
- **Testy regresyjne** promptów (testy agenta w ElevenLabs albo pytest).

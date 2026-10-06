# ⚙️ MARK LV (55)
### The Ultimate Cross-Platform Personal AI Assistant — By Ankit Meena

> 📺 **[Watch the full setup video on YouTube](https://www.youtube.com/@ankitmeena)**

A real-time voice AI that can hear, see, speak, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

Mark LIV gave the assistant a face. **Mark LV gives it a screen — and the stamina to keep working when a model goes down.**

Say "play the new Dune trailer" and the video appears **where the avatar was**, in the HUD itself, muted until you ask for sound. A YouTube link, a local file, a direct URL or just a description to search for: they all land in the same place, and JARVIS tells you it is coming *before* the picture arrives instead of going quiet for five seconds.

Underneath, every Gemini call in the app now goes through **one ladder of nine models**, ordered by measured response time rather than guesswork. When a model hits its quota, times out or disappears, the next rung takes the call and the dead one is put on a cooldown so nobody pays for it twice. Before this release, sixteen files named their own model, twenty-six times, with **no timeout at all** — one unwell alias could hang a request forever.

The release also draws a line through the bundled skill list. **Everything JARVIS ships with now drives the computer** — applications, files, the browser, the desktop, the screen. Five skills that served one hobby or one trade left the list, which is a thousand tokens the model no longer reads on every connection, and five fewer wrong tools for it to reach for.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🧑‍🎤 Holographic Avatar | An animated human head in the HUD — real facial geometry, lit and drawn in software, no GPU or extra packages |
| 👄 Real Lip-Sync | ~50 mouth shapes a second from the audio's formants **and** the transcript — closures, spreads and rounds, not a volume meter |
| 🌍 Language-Free Mouth | Articulation is derived by Unicode reduction, so Latin, Cyrillic and Greek scripts all work from one rule set — and scripts that hide pronunciation fall back cleanly |
| 🙂 Facial Acting | Brows track the phrase, gaze saccades between fixations, natural blinking, a small nod on stressed syllables |
| 😐 Face as Status | Looks away while thinking, meets your eyes while listening, lids fall while asleep, glances down at new content |
| 🎚️ Push-to-Talk | Hold **Ctrl+Space** and the mic opens — closed the rest of the time. Truly global on Windows, window-scoped elsewhere |
| 🔇 Self-Echo Guard | Never answers its own last sentence: the tail of its own voice is recognised and dropped without muting you |
| 🪪 Runtime Self-Knowledge | Name, OS, abilities **and limits** are generated from the live system each session — rename it or add a plugin and it knows |
| 🎙️ Wake Word | Local **"Hey Jarvis"** detection — sleeps until called, auto-sleeps after 2 min of silence, and never streams audio while asleep |
| ⚡ Instant Acknowledgment | Speaks a short, context-aware reply in **your language** the instant a longer task starts — no more silent waiting |
| 🚀 Faster Live Engine | Runs on **Gemini 3.1 Flash Live** — roughly 2× faster time-to-first-word than the previous model |
| 🧩 Self-Describing Skills | Every bundled skill declares its own `TOOL` dict + handler and is auto-discovered at launch — adding one is a single file |
| 🧠 Recallable Memory | No size limit and nothing silently forgotten — the prompt carries what fits, the rest is looked up on demand from a local search |
| 👁️ Memory Panel | See every fact JARVIS has stored about you, when it learned it, and delete any of it in one click |
| ↩️ Undo | Take back what the assistant did — files it moved, renamed, created or wrote, and settings it changed |
| ⚠️ Real Confirmation | Shutdown, restart and WiFi wait for a button **you** press — the model cannot confirm its own irreversible actions |
| 🎧 Audio Device Picker | Choose the microphone and speakers by name, filtered to the short list your OS shows — and measured, so every entry actually works |
| 🔗 Session Continuity | A dropped connection, a voice change or a device change no longer wipes the conversation |
| 🧩 Plugin System | Drop a single `.py` file into `plugins/` — JARVIS learns a new skill on next launch |
| 📺 Video on the HUD | Plays YouTube, a local file or any video URL **where the avatar sits** — starts muted, sound on request |
| 🪜 Model Ladder | Nine Gemini models in one measured order — a quota, a timeout or an outage steps to the next rung instead of failing |
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — the avatar retints with it |
| 〰️ Reactive HUD | Waveform pulses to real audio — your mic while listening, JARVIS while speaking |
| 🎙️ Voice Picker | Choose from 5 native Gemini voices and switch live from the UI — no restart |
| ♾️ Unlimited Sessions | Sliding-window context compression — one conversation can last for hours |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Screen capture and webcam vision piped into your main Gemini session, labelled by source |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🪪 Assistant Customization | Change the assistant name, your name, voice, and colour from the UI — takes effect immediately |

---

## 🆕 What's New in Mark LV

One new dependency for the whole release — `yt-dlp`, and only to turn a YouTube page into something Qt can play.

### The display

#### 📺 Video where the face is
The HUD already had a surface that takes the centre of the screen and gives it back: the camera. Video shares that same stack, so the avatar, the live camera and a video can never be on screen at once — and a video always lands exactly where you are already looking.

It accepts **a local file, a direct media URL, a YouTube link, or just a description** ("play the new Dune trailer") and searches for it. It **starts muted every time**, because a soundtrack talking over the assistant is the one way this feature could make JARVIS worse rather than better. You turn the sound on by asking, or from the button in the video header.

**It answers before it opens.** Resolving what to play takes seconds nothing can remove — measured at 2.0s for a link and 3.3s for a spoken phrase, plus buffering. Restricting yt-dlp to a lighter client was tried and made it worse: the fast clients came back with zero usable formats. So the seconds stay, and what changed is where you spend them — listening to JARVIS say it is coming, instead of watching nothing happen. Say "stop" during those seconds and the video is cancelled before it ever reaches the screen.

#### 🔀 Two streams, one picture
The first version asked YouTube for a format carrying both picture and sound and got *"Requested format is not available"*. That was not a bad selector; it was a wrong assumption. Checked against three videos — including the oldest upload on the site — **every one offered zero combined formats.** Picture and sound are separate streams now.

So there are two players running together, with a timer that corrects any drift over 300 ms. And the audio track is chosen on **language first, bitrate second** — because YouTube auto-dubs a great many videos and ships every dub at the *same* bitrate as the original: measured on one video, English at 129.483 and Arabic, Bangla, German, Spanish, French, Hindi and Indonesian all at 129.482. Sorting on bitrate alone came down to a thousandth of a kilobit, so the same video would play in Arabic one time and English the next for no reason you could see. It now reads YouTube's own original-track marker first.

#### 🔇 It stopped hearing the film
The microphone is open while a video plays, so the moment you turn the sound on, JARVIS starts answering the film. The mic now mutes itself when the video's sound goes on and unmutes when it goes off — and it says so in the activity log rather than going deaf silently.

#### 🎛 Two drawers instead of one
⚙ **SETUP** holds the things you set once — remote control, desktop shortcut, auto-start and customisation. 🎛 **CONTROLS** holds the switches you flick daily — fullscreen, morning brief, wake word, sleep, push-to-talk, HUD style. Only one is open at a time, and each sits under its own header button.

The panel used to stutter when it opened, and the obvious explanation — too many buttons — was wrong. Measured, the **first** wake-word state check took **2.104 seconds**, because it imported `openwakeword` on the UI thread the moment the drawer was built. That import now happens off-thread at boot, and the drawer opens instantly whether it has six buttons or sixteen.

> The drawer also used to vanish *behind* the video. `QVideoWidget` creates a native child window, and no Qt overlay can be drawn on top of one. The video is now a `QGraphicsVideoItem` inside a graphics view — same picture, and the interface stays where it belongs.

### Staying up

#### 🪜 Every Gemini model, in one ladder
Every one-shot Gemini call in the app now goes through `core/gemini.py`. Before this, **sixteen files named their own model — twenty-six times — and not one of them set a timeout.** When a single alias went unwell, the call did not fail; it hung.

The ladder is nine models deep, ordered by **measurement rather than guesswork**:

| Model | Measured | Model | Measured |
|---|---|---|---|
| `gemini-3.5-flash-lite` | 0.56s | `gemini-2.5-flash` | 0.67s |
| `gemini-3.1-flash-lite` | 0.60s | `gemini-2.5-flash-lite` | 0.74s |
| `gemini-flash-lite-latest` | 0.60s | `gemini-3.5-flash` | 1.13s |
| `gemini-3-flash-preview` | 504 after 14.7s | `gemini-3.6-flash` | 504 after 12.0s |
| `gemini-flash-latest` | 503 UNAVAILABLE | | |

The three that fail were **not deleted** — a model that is unwell today is a real rung tomorrow. They sit at the bottom, and a **cooldown** decides how long a failure is believed:

| What happened | Rested for | Why |
|---|---|---|
| Quota exhausted (429) | 5 minutes | Quotas refill |
| No answer (503 / 504 / DEADLINE_EXCEEDED) | 30 minutes | An outage outlasts a retry |
| Not found, or no access (404) | 6 hours | Your key does not have it, and won't in a minute |

That middle row is where the time was going. Only quota and 404 used to be cooled, so the 14-second wait on a dead model was paid **on every single call**. Measured after the fix: first call 13.15s, second 1.98s, third 1.16s — **11.2 seconds saved on every call from then on.**

**The Live model is deliberately not on this ladder.** Live models are not drop-in replacements for one another; they accept different config fields, and the same API key also exposes transcribe-live, live-translate and robotics-streaming models that will happily connect and then not behave like an assistant. So Live has exactly **two** rungs — the current model, and the one this project used before it — and it steps only on quota or loss of access. Never on a network blip or a bad key, which would otherwise walk the whole list into the same wall.

### The shape

#### 🧩 Everything bundled drives the computer
The bundled skill list had grown to seventeen, and some of it was nobody's business but its author's. **Not everyone updates games; everyone opens applications.**

Mark LV ships **76** self-describing skills (every `actions/*.py` that declares — or re-exports — a `TOOL` dict), across 80 files in `actions/`; the other four are support modules the skills call into. They fall into two honest groups: driving this machine — applications, the browser, files, the desktop, the screen — and fetching for it — search, weather, flights, video. The rule is written into the project tree, so the next skill lands in the right folder without anyone having to ask.

**Every one of those 76 declares itself to the model on every connection**, whether you ever use it or not. That is the cost of the plugin architecture, and it is not small: the declarations alone are **72,263 characters — roughly 18,000 tokens** — before a word is spoken, and 76 tools is a lot of surface for the model to pick the wrong one from.

> Measure it yourself rather than trusting this paragraph — the numbers above are generated, not remembered:
>
> ```bash
> python tools/count_tools.py
> ```
>
> **This is the next thing worth fixing in JARVIS.** Tiering the declarations — a small always-loaded core plus an on-demand `find_tools` lookup — would cut the per-connection payload by an estimated 70% and should also reduce wrong-tool selection. It is listed in `PROJECT_ANALYSIS.md` as the single highest-value change outstanding.

### 🩹 Fixes
* An unanswering model was retried on **every call**, at 12–15 seconds a time, because only quota and 404 failures were ever cooled down. 503/504 now rest for 30 minutes — **11.2 seconds saved per call**.
* One-shot Gemini calls had **no timeout anywhere**, in any of the sixteen files that made them. Every call now carries a deadline of at least 10 seconds.
* YouTube playback failed outright with *"Requested format is not available"* — it was asking for a combined stream that no longer exists.
* Videos played in a **random language**, because YouTube's auto-dubs carry the same bitrate as the original track.
* The settings drawer **stuttered on first open** — a 2.1-second `openwakeword` import on the UI thread, not the button count it looked like.
* The settings drawer opened **underneath the video**, because `QVideoWidget` creates a native window.
* JARVIS **answered the video's soundtrack.** The microphone now follows the video's sound.
* Qt's multimedia backend printed an ffmpeg banner to the console on every play, containing the **signed streaming URL with the viewer's IP address in it**. Silenced at startup.
* **Every launch paid 201 ms for a plugin nobody had asked to use.** Discovery executes every file in `plugins/`, and `youtube_video` imported `requests` and `youtube_transcript_api` at module scope. Deferring one of them would have saved nothing — the transcript library imports `requests` itself. Both are now checked with `find_spec`, which answers "is it installed?" without executing anything, and loaded on first use: **plugin discovery 211 ms → 39 ms**.
* A plugin that needed a file from a **newer Mark** was rejected with *"pip install core"*. The loader could not tell this project's own packages from a third-party one, so it told people to install a same-named stranger from an index — wrong, and a supply-chain hazard dressed up as a fix. First-party names now say the app is behind the plugin and that there is nothing to install.

> Built on the Mark LI–LIV foundation: the **🧑‍🎤 Holographic Avatar**, **👄 Lip-Sync**, **🎚️ Push-to-Talk**, **🔇 Self-Echo Guard**, **🧩 Plugin System**, **♾️ Unlimited Sessions**, **🎨 Live Theming** and **🎙️ Wake Word** are all still here.

---

## 🔄 The Foundation Update — in every Mark from LII

These four landed across **Mark LII, LIII, LIV and LV at the same time**, after each of those releases had already shipped. They are not what any one of those versions originally introduced; they are the floor all of them now stand on, so moving up a Mark never costs you something the one below it had.

No new dependencies. No bundled asset files. No hardcoded language, and nothing that assumes one operating system.

### 🧠 A memory that actually remembers

The store was capped at **2,200 characters — the whole memory, not per entry** — because all of it was pasted into the system prompt on every connect, so growing the memory grew every request. When it filled, the oldest entries were deleted and one line was printed to a console nobody reads. An assistant advertised as remembering "projects, preferences and personal context" was in practice a two-page notepad that quietly forgot your sister's name after a few weeks.

Storage and prompt budget are now separate problems:

* **Nothing is deleted.** The cap is a runaway guard normal use never approaches, and if it is ever hit it says so in the activity log instead of on stdout.
* **The prompt carries a core, not a dump.** Identity in full, then the most recently updated facts, budgeted — measured at **971 characters on a memory holding 62 stored facts.** That is *smaller* than the old whole-store cap, so sessions now connect with fewer tokens than before.
* **The rest is fetched on demand.** A `recall_memory` tool searches the full store locally — no network, no second model, well under a millisecond.

The part that is easy to get wrong: **a model cannot look something up if it doesn't know the thing exists.** So the prompt also carries an **index of the keys** it had no room for. Without it, "who is Ayşe?" gets "I don't know" while `ayse_sister` sits on disk unread. That index interleaves categories rather than sorting by recency — sorted like the core, a memory with forty preferences pushed the one entry the index existed for off the end.

⚙ → **🧠 MEMORY** shows every stored fact, when it was learned, and a ✕ to forget it. Everything stays in `memory/long_term.json` on your machine.

### ↩️ Undo — it can take back what it did

JARVIS moves files, renames them, writes to them and changes your settings. None of that had a way back; if it misheard you, the only remedy was to fix it by hand.

Say **"undo"** — in any language — and it reverses its own last action:

| | |
|---|---|
| **Files** | move · rename · create · copy · write · delete · organize desktop |
| **Settings** | volume · brightness · dark mode |

Three things it deliberately does *not* do:

* **It does not guess.** Settings undo reads the current value *before* changing it. Where a platform won't report that value, nothing is registered — an undo that restores a guess is worse than no undo.
* **It does not hoard.** Undoing a write means keeping the old contents in memory, so files over 1 MB are excluded and it says so rather than holding a 200 MB log for the session.
* **It does not delete your files to undo a copy.** The reverse of a copy is removing the copy; the reverse of "create a folder" is removing it *only while it's still empty*.

`organize_desktop` gets special treatment — one command that moves dozens of files, which made it the least reversible thing the assistant could do. It journals every move and puts all of them back in one go, cleaning up the folders it created if they're still empty.

**Undo costs nothing at runtime.** It appends a closure to a list; nothing in it runs unless you ask.

### ⚠️ A confirmation the model can't forge

The old gate read like this:

```python
if action in _DANGEROUS_ACTIONS:            # {"restart", "shutdown"}
    confirmed = str(params.get("confirmed", "")).lower()
```

`confirmed` is a **tool parameter, which means the model fills it in.** Nothing stopped it sending `confirmed=yes` on the first call and nothing checked that a human was ever involved. It was a convention, not a gate. And its coverage was two actions — so `toggle_wifi`, which cuts the assistant's own connection to the Live API and therefore *cannot be asked to undo itself*, went through with no gate at all.

The token is now issued by the interface. Shutdown, restart and WiFi put a banner on the HUD and **return immediately**; the action runs only if you press CONFIRM. Nothing blocks — JARVIS keeps talking while the banner is up — so this is **cheaper than the old gate**, which burned two tool round trips on every power command.

> The split between the two mechanisms is about reversibility, not about how alarming a word sounds. Anything undoable is done at once; only the genuinely irreversible asks. An assistant that checks with you before turning the volume down is one you stop talking to.

### 🎧 It finally asks which microphone

Both audio streams opened with no device argument at all, so they always took whatever the OS called "default" — and on Windows that *moves on its own* the moment you plug a headset in. "JARVIS can't hear me" almost always meant "JARVIS is listening to the webcam".

⚙ → **🎧 AUDIO DEVICES** lets you pick the microphone and the speakers by name. Two things matter more than the dropdown:

**The list is short.** `query_devices()` returns one entry per *device × host API*, not per device — measured on an ordinary Windows machine, **41 entries for what the sound settings show as 4 microphones and 4 speakers.** The same microphone appears four times, under MME, DirectSound, WASAPI and WDM-KS, with nothing to say which is which. That is not a choice, it's a quiz. The picker takes one host API per direction, drops the "Sound Mapper" and "Primary Sound Driver" pseudo-devices that just mean "default", and deduplicates. **41 → 8.**

**Every entry has been measured, not assumed.** The obvious approach is to pick the host API with the nicest names — WASAPI on Windows, which in shared mode **doesn't resample**, so with 16 kHz in and 24 kHz out against 48 kHz hardware every open failed. Adding a rate check and moving to DirectSound passes that test on both sides, and PortAudio's DirectSound **output is a silent sink**: the stream opens, every write returns success in ~0 ms, and not one sample reaches the speakers.

| | write(2.0 s) took | |
|---|---|---|
| MME | **2.02 s** | consumed in real time |
| DirectSound | **0.00 s** | swallowed instantly |

No capability flag reports that. So the app measures it — once per host API per direction, on a background thread at startup, using silence. Two consequences worth stating plainly:

* **Each direction picks its own host API.** On Windows this lands on DirectSound for the microphone and MME for the speakers — a split no amount of reasoning would have produced.
* **The probe runs in the mode the app actually ships.** DirectSound input passes a callback stream and fails a blocking read; probing the wrong mode rejected a microphone that works perfectly.

Your choice is stored **by name, not by index** — indices shift whenever something is plugged in. If the saved device is gone, it falls back to the system default and says so in the log rather than failing to start.

### 🔗 It stops forgetting the conversation when the connection drops

`session_resumption` was switched on in the config and the handle the server sent back was **never read** — so every reconnect started an empty session. A dropped packet, or simply changing the voice, wiped the conversation. "Unlimited sessions" leaked through exactly this hole.

The handle is captured and replayed now. A network blip, or switching your microphone, keeps the conversation intact.

It is held in memory only, deliberately: writing it to disk would make a fresh launch continue yesterday's chat, which sounds appealing but breaks the session-summary flow — a conversation that never ends never produces a summary, and the "yesterday we talked about…" line in the morning briefing silently disappears. Changing the **voice** also starts clean on purpose, since resuming restores the server's session state and would likely bring the old voice back with it.

### 🩹 Fixes that came with it

* **The assistant could die on a log line.** Status lines carry emoji and arrows (`📤 file_controller → Moved: a.txt → Documents/`). On a non-UTF-8 console — cp1254 on a Turkish Windows, cp1251 on a Russian one, cp932 on a Japanese one — printing one raises `UnicodeEncodeError`, and because that print sits *after* the tool's own `try/except`, it escaped into the receive loop and took the session down.
* **Every computer command paid for two model round trips.** `computer_settings` made an *entire second Gemini call, inside the tool*, purely to translate the request into one of its own action names — because the declaration only said "The action to perform", so the model rarely filled it in. When that second call failed, the fallback was `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it down" into `sesi_kis` and straight into "Unknown action". The declaration now names all 56 actions and the rest is spelling tolerance handled locally by `difflib` in microseconds. When nothing matches it suggests real action names instead of dead-ending.
* An unresolvable saved audio device, or one the driver refuses to open, falls back to the system default and says so — on both the microphone and the speakers.
* A rejected session-resumption handle is dropped after one attempt, so an expired handle can never be replayed on every retry and prevent the reconnect it exists to protect.



---

## 🤖 The Agentic Update — Mark LV+

### What JARVIS now does on its own

| Feature | Description |
|---|---|
| 🕐 **Mission Control** | Every tool call the session makes — time, arguments, duration, success/failure — lands on a live timeline. Ask `mission_control` for `timeline`, `stats` or `clear`, or open the dashboard's **🎮 Activity** tab |
| 🧠 **Agentic Task Engine** | Give a goal ("clean my downloads folder"), a planner breaks it into tool calls, the orchestrator executes each step with stop-on-failure, retry and a written report. Steps dispatch through the same registry the model uses, so an agent can never reach a tool the session doesn't have |
| 🤖 **Rules & Automation** | `when 18:00 → system_monitor`, `when report.pdf appears in ~/Downloads → file_processor`, `when I say "movie mode" → video_player`, USB plug/unplug triggers — time, file-event, phrase and device triggers that fire real actions, with self-healing retries. This IS JARVIS's scheduled-NL-automation layer ("every day at 8 say X" = a time rule), so a separate schedule engine would duplicate it |
| 🎬 **Macro Recorder** | Record a screen macro, replay it later. Replay is gated behind `confirm=yes` and a 400-event safety cap |
| 🔍 **Scanner upgrades** | Five new modes: `dupes` (MD5 duplicate finder), `treemap` (folder-size bars), `speed` (Cloudflare 10 MB speed test), `drives` (partitions), `startup` (autostart audit) |
| 🪟 **Window Layouts** | `split` / `coding` / `stack` / `left` / `right` / `center` — pure geometry with xdotool, win32 and AppleScript backends |
| 📋 **Clipboard History** | A watcher keeps the last 100 clips — `clip_history` list / search / use / clear |
| 🌐 **Scrape** | URL → clean text, links and title. bs4 when present, stdlib fallback when not, non-HTTP schemes rejected, same cached httpx client as everything else |
| 🔬 **Research → Report** | Multi-query DDG fan-out → fetch top pages → Gemini-written markdown report with numbered citations (extractive fallback without a key) → saved to `research/<topic>-<ts>.md` |
| 📊 **Charts & Diagrams** | `chart bar "CPU=42, RAM=68"` and `diagram flow "Voice → Wake word → Gemini"` — pure-SVG output, no matplotlib |
| ⏱ **Focus Sessions** | Pomodoro rounds that mute proactive check-ins while you work |
| 🔪 **Process Manager** | List/kill with `confirm=yes`, refuses to kill its own tree |
| 🛣 **Screen Mirror** | Dashboard 🖥️ button **or voice** ("mirror my screen" → `screen_mirror start/stop/status`) streams a low-res live view of the PC to the phone |
| ☁️ **Git Snapshots** | Every successful `dev_agent` build commits itself — argv-only git, identity env-pinned, failure never fails the build |
| ⏰ **Reminder List/Cancel** | `reminder action=list` shows upcoming scheduled reminders, `action=cancel 2` removes one |
| 🌡 **Hourly Weather** | "Kaisa rahega aaj ka weather *next hours*" → a 12-hour strip alongside the daily forecast |
| 🎬 **Scenes** | Rules grew multi-step: *'movie mode'* can open the player, set volume and start a focus session in order — stop on first failure, `scene(N steps)` in the rules list |
| 🔡 **Live Region OCR** | `region_ocr` reads the live screen region (presets or `x,y,w,h`) — tesseract offline, Gemini vision next; `repeat` watches a value and reports only changes |
| 📧 **Gmail** | Bundled plugin over IMAP/SMTP with an app password (stdlib only) — unread/list/read/send/search + guided setup with a live login check |
| 📅 **Calendar** | Bundled plugin reading any ICS feed (Google Calendar's secret iCal URL) — upcoming/today/explicit date, RFC 5545 unfold + daily/weekly/monthly recurrence, bounded expansion |
| 📷 **Phone → Vision** | The phone dashboard's 📷 button streams rear-camera JPEG frames to `/api/camera-frame`; `phone_vision` then answers *'what's on my desk'*, OCRs the frame or describes it — with a 5-minute freshness gate |
| 🎙 **Meeting Recorder** | `meeting start/stop` records the mic to WAV, transcribes offline (faster-whisper), saves a `.txt` next to it and can summarise decisions/action items; 60-minute auto-cap; decisions auto-push to calendar events and reminders |
| 🔒 **Privacy Mode** | `privacy on` — cloud-facing tools (search, research, scrape, vision, translate, file analysis, monitors, flights) refuse with an honest message instead of transmitting; memory, history, RAG, automation and LAN MQTT keep working |
| 🕵️ **History Search** | Local sqlite FTS5 over past conversations — survives restarts; *“what did I say about that laptop”* answers from the actual exchange |
| 📚 **Local RAG** | `rag index path=…` then ask — your text/markdown/code/CSV files (PDF/DOCX via file_processor), offline, no API |
| 🔌 **MCP Client** | `mcp list/call` — tools from any Model Context Protocol server (filesystem, GitHub, Slack, Notion, …) over **both standard transports: stdio and Streamable HTTP** (`url=` + auth headers) |
| 🦜 **Live Translate** | `translate live from=en to=hi` — offline Argos pair first, Gemini when online, honest refusal when neither can |
| 🧩 **Event-Driven Rules** | File-appears, USB-plug, phrase and time events reach the rules engine through a shared event bus — not just polling |
| 💾 **Task Persistence** | Agentic runs live in sqlite — `task_agent` lists them and *“resume task N”* skips completed steps (idempotent) |
| 📊 **DuckDB SQL** | `data file=report.csv query=SELECT …` — SQL over CSV/Parquet/JSON locally, schema on demand, LIMIT auto-applied |
| 🔐 **Password Vault** | AES-256-GCM behind one master passphrase — unlock/set/get/list, locks itself after idle |
| 🖥 **Sandboxed Terminal & Git** | Allowlisted tools only (python, pytest, ruff, npm, git, …) as argv — no shell operators, repo-confined, timeout-capped |
| 🧊 **3D Models** | `make_3d spec='box 40 20 8; cylinder 6 12'` → STL/OBJ mesh, previewed on the 3D surface |
| 🔁 **Auto Barge-In** | EchoGuard: sustained-evidence gate over `interrupt()` — you can talk over a reply; the echo tail or room noise can't |
| 🎥 **Motion Detect** | The phone-camera pipe doubles as a security cam — motion alerts stream to the dashboard |
| 🗂 **Render Surfaces** | Everything routed: CHAT · DISPLAY · SCAN · 3D · WEB tabs on the dashboard, classified from the content title |
| 🌐 **Agentic Browser** | `browser_control action=agent goal=…` — EXTRACT → REASON → ACT → VERIFY with live page snapshots until the goal is met |
| 👥 **Multi-Agent Dry Run** | `multi_agent task=…` — planner → coder → tester emit per-file diffs and verdicts; nothing is written until `apply=true` |
| 🙋 **Presence Detection** | Input-idle + native OS idle probe → present/away with hysteresis; proactive speech stays quiet in an empty room |
| 🏠 **Smart Home (MQTT)** | `smart_home pub/sub/status` against Home Assistant, zigbee2mqtt, Tasmota, ESPHome — internet brokers blocked under privacy |
| 🌲 **Code Outline** | `code_outline file=…` — tree-sitter AST symbols with line numbers and nesting; stdlib `ast` for .py, labelled approximate scan elsewhere |
| 🤖 **Local LLM by Default** | Ollama + llama3.2 on localhost:11434 out of the box; LM Studio / LocalAI / Jan aliases normalize to the OpenAI-compatible path |
| 🧠 **Replanning Brain** | The planner asks before it guesses (ambiguous goals → one clarifying question, nothing runs) and re-plans up to twice when a step fails — bounded, same-plan loops detected and stopped, everything auditable in the report |
| 🛑 **Voice Cancel** | "Stop the task" mid-run: cooperative cancellation at the next step boundary, status `cancelled`, plan + finished steps kept — `task_agent action=resume` picks up where it stopped |
| 🕹 **Autonomy Modes** | `observe` (mutating tools refuse — watch only) · `ask` (confirm per action) · `auto` (confirmed actions enhance themselves), with TTL so auto forgets itself; `shutdown`, `send_message`, vault and macro stay manual in every mode |
| 🔌 **MCP Native Tools** | Every tool on every configured MCP server appears as a first-class `mcp__server__tool` in the session — no JSON round trip, TTL-cached declarations, dead servers simply absent, sanitised names with collision suffixes |
| 🌐 **MCP over Streamable HTTP** | The second standard MCP transport (spec 2025-11-25): `mcp action=add name=x url=…` with auth headers — SSE or JSON answers, session-id echo, transparent re-init on 404, DELETE on close |
| 🖱 **GUI Agent** | "Enable dark mode for me" → screenshots, decides the next click/type/scroll (preview first, acts only when allowed), verifies after every step, detects stuck loops, respects privacy and cancel; `gui_provider=ollama` runs the vision brain fully local |
| 🪞 **Self-Improving Rules** | `rules action=suggest` mines your own history and task runs for repeats (3+ days / hour-clustered runs) and hands you the exact `rules add` line — it never installs anything itself |
| ⏳ **Interval Triggers** | `when every 2h → …` joins time/file/phrase/USB triggers; anchored on disk so restarts don't reset the clock, rejected up-front if the value isn't `30m/2h/1d/1w` |
| 🔄 **Crash Recovery** | A run interrupted by a kill/kill-switch is swept at boot into resumable `cancelled` — plan and completed steps intact, one honest console line, `resume` continues |
| 📜 **Recency-Weighted Recall** | History search re-ranks FTS results by relevance × freshness — a fresh equal answer wins, a strong old answer still beats a weak fresh one |
| 🛰 **World View** | Satellite (NASA GIBS VIIRS true colour — free, no key) or street map (OpenStreetMap) of any lat/lon, stitched to a PNG on the HUD; the mirror tool toggles the phone's live PC view by voice |

### The guarantees behind it

- **Orchestrator** — destructive steps (`shutdown`, `delete`, `kill`, `macro replay`…) refuse to run unless explicitly allowed; the first failure stops the task and the report says so.
- **Rules** — phrase triggers can't nest-loop; file triggers reset by mtime AND date; every rule fire is logged to the UI.
- **Macro replay** — requires `confirm=yes`, capped at 400 events, aborts on mismatch, never runs without a visible safety line.
- **Kill** — needs `confirm=yes`, never touches the assistant's own process tree.
- **Everything is offline-tested** — 140 tests across `tests/` cover the whole layer with fake clocks, fake planners and temporary directories; CI runs them on Python 3.11/3.12/3.13.

---

## 🖥️ The HUD v2 — the desktop app was rebuilt

The PyQt HUD (`ui.py`) is a complete redesign on one design system — vector
icons everywhere (zero emoji in the chrome), a five-colour accent that themes
the whole window, and every panel wired to the **same dashboard API the web UI
uses**, so both front-ends show live data instead of mockups.

| Area | What it does now |
|---|---|
| **Spaces** | Create spaces/pages and **edit page markdown in place** — SAVE carries `base_rev`, so a Dot's proposal can never clobber your typing (409 → toast + reload) |
| **Agents** | Create, edit and delete Dots from the panel (delete asks for confirmation), full per-Dot chat + memory + monitor feed |
| **Computers** | Four sub-surfaces: **SHELL** (argv-only audited commands), **FILES** (jail-scoped list/read/write), **BROWSER** (navigate · read · snapshot · shot · click · type · key · scroll), **SCREEN** (live screenshot viewer) |
| **Calls** | Start/end live calls with toasts, transcript, captions — plus a **background instruction** (`POST /api/calls/{id}/background`) that keeps running between turns |
| **Research** | Every research result lands as a page; the page list badges `[research · n src]` and the viewer prints the full **SOURCES** receipt at the bottom |
| **Agenda / Memory / Skills** | Task pause/resume/cancel/retry toasts, memory CRUD toasts, skill **DETAIL** dialog (body, provenance, dates) |
| **Navigation** | **Ctrl+K** command palette (type-to-filter, Enter jumps), **Ctrl+1…9 / Ctrl+0** for direct section jumps, chat **QUICK ACTIONS** row (status · agenda · research · remember · scan) |

The web dashboard got the same treatment: nav, composer and file chips now
render inline SVG instead of emoji, through a shared `JV.svg()` registry.

---

## 🗺️ Mark Roadmap

| Mark | Focus |
|---|---|
| **XLIX** | Auto-start · clipboard intelligence · assistant customization |
| **L** | Session memory · background monitoring · proactive 2.0 · instant vision |
| **LI** | Plugin system · affective dialog · proactive audio · unlimited sessions |
| **LII** | Voice picker · live theming · reactive HUD · recallable memory · undo · real confirmation · audio device picker · session continuity |
| **LIII** | Wake word · Gemini 3.1 Flash Live · instant acknowledgment · self-describing action/plugin architecture |
| **LIV** | Holographic avatar · viseme lip-sync · facial acting · face-as-status · push-to-talk · self-echo guard · runtime self-knowledge & limits |
| **LV** | Video on the HUD · model ladder with measured fallback · split settings drawers · trimmed bundled skill list |
| *shared* | The last five above also shipped to LIII, LIV and LV at the same time — moving up a Mark never loses them |
| **LVI+** | Interrupt by voice · conversation history · Telegram remote · full file access · security camera · Obsidian |

---

## 🛰️ The Integration Update — everything wired in

Twenty batches, every one landed with full-suite gates (ruff · pytest ·
HUD smoke) green before push. Nothing below is a stub: each feature
either runs for real or returns the exact free-install line — never a
fake result.

| Area | What shipped |
|---|---|
| **Knowledge** | Graphiti-lite temporal graph (`graph`: validity windows, offline extraction, multi-hop search) · skills **verify-loop** (lint on draft, re-run on publish, sweep endpoint) · deep-**research critic** (authority-ranked pruning, arXiv/Semantic Scholar/Wikipedia/GitHub sources, citation-precision header) |
| **Creation** | **CadQuery** CAD (snippet → STL/STEP/SVG) · **Manim** animations (scene → mp4) · **Mermaid** render (real mermaid-cli → SVG) — all guarded, honest install lines |
| **Understanding** | **Video Q&A + SRT captions** (ffmpeg → whisper segments) with optional **WhisperX** engine (word timings, diarization; honest fallback) · **dictation** typing mode (offline segments + live transcript feed) · **emotion-tag acting** (reply tone drives the avatar's face) |
| **Search & dev** | **SearXNG** self-hosted metasearch as a real rung in the search ladder (config `searxng_url` / env `SEARXNG_URL`) · **dev_loop** background edit→test watcher (pytest/npm auto-detect, PASS/FAIL log) · **eval A/B** reply judging with persisted scorecards |
| **Remote & streams** | **Telegram** closed loop: receive (`telegram_rx`, allowlist-gated) + send (`telegram_send`, headless Bot API) · **predictions** ground-truth loop (annotate → HUD chip → Telegram verdict) · **go2rtc** RTSP/IP-camera bridge (official-release install, start/stop/status) · **WebRTC mirror** upgrade over the existing screen mirror (aiortc; JPEG fallback always intact) |
| **Platform** | Dashboard as an installable **PWA** (manifest, service worker, offline shell) + **Web Push** with VAPID keys and RFC 8291 encryption, zero new dependencies · **MCP catalog** (11 official presets) · **Obsidian** Local-REST vault · **AT-SPI** accessibility scanner · **self-healing browser locators** (fallback ladder + learned locator hints) · **proactive 3.0** (pending/expired decisions injected into check-ins) |

Duplicate rule held throughout: barge-in was found already shipped (EchoGuard) and skipped rather than rebuilt.

## ⚡ Quick Start

```bash
git clone https://github.com/tonystark302012-rgb/jarvis.git
cd jarvis
python setup.py        # installs deps for YOUR OS + the browser automation engine
python main.py
```

`setup.py` only ever installs what your operating system needs — the Windows-only libraries are skipped automatically on macOS and Linux, and vice-versa. It also checks your Python version up front, so a wrong interpreter fails with a sentence instead of a wall of pip output. Prefer to do it by hand? `pip install -r requirements.txt` works too.

> ⚠️ **Installation Note:** If you hit a `ModuleNotFoundError` for an OS-specific package, install it with `pip install <module_name>`. The optional **wake word** engine is *not* installed here — grab it in one click from **⚙ → WAKE WORD** inside the app.

---

## 🛠️ Development

```bash
pip install -r requirements-dev.txt   # pytest + ruff + test deps (fast, no PyQt needed)
ruff check .                          # lint — pyflakes + statement errors, must be clean
python -m pytest tests/ -q            # full suite — offline, no mic/display/API key
python tools/ui_smoke.py              # offscreen HUD E2E (needs PyQt6 + a display server stub)
python tools/ui_preview.py            # bootstrap the dashboard preview on :8712 with a PREVIEW token
python tools/feature_audit.py         # live probe: every feature's real entry point (19 checks, exit≠0 = broken)
```

CI runs both on every push and pull request across **Python 3.11 / 3.12 / 3.13** (`.github/workflows/ci.yml`). The suite covers the security invariants: no `shell=True` in `open_app`/`dev_agent`, the run-command allowlist, project-path containment, pip-flag injection, the dashboard AES round-trip, brute-force lockout, memory recall and parallel tool dispatch.

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11, 3.12 or 3.13 |
| **Microphone** | Required for voice interaction (and for the "Hey Jarvis" wake word) |
| **Speakers** | Required for voice replies |
| **API Key** | Free Gemini API key (entered on first launch → `config/api_keys.json`) |
| **GPU** | **Not required.** The avatar is rendered in software, and so is HUD video |
| **YouTube on the HUD** | `yt-dlp`, installed by `setup.py`. Without it, local files and direct URLs still play and YouTube links open in the browser with an explanation |
| **Wake word** *(optional)* | One-click download from ⚙ → WAKE WORD (`openwakeword`, a few MB, fully local) |

---

## 🗂️ Project Structure

```
jarvis/
├── main.py                   # Core loop — Gemini Live session, audio I/O, viseme extraction, tool dispatch
├── ui.py                     # PyQt6 HUD v2 — vector-icon design system, live dashboard panels, palette
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies, checks your Python)
├── pyproject.toml            # ruff + pytest configuration (lint must stay clean in CI)
├── requirements.txt          # Runtime dependencies (OS markers filter per platform)
├── requirements-dev.txt      # Test/lint dependencies — pip install -r requirements-dev.txt
├── .github/workflows/ci.yml  # CI: ruff + compileall + pytest on Python 3.11/3.12/3.13
├── .gitignore                # Keeps your API key, TLS key and memories out of the repository
├── tests/
│   ├── test_roadmap.py       # Biggest suite — every roadmap batch: tools, privacy, presence, agents
│   ├── test_upgrades.py      # Offline suite — security invariants, memory recall, dispatch, dashboard
│   ├── test_new_features.py  # Mission Control, orchestrator, rules, mirror, layouts, lifecycles
│   └── test_ui_features.py   # source pins for the HUD v2 feature set + dashboard vector icons
├── tools/
│   ├── ui_smoke.py           # offscreen E2E smoke — construction, panels, routing, 115 checks
│   └── ui_preview.py         # dashboard preview bootstrap (token + pin for the test harness)
├── plugins/
│   └── _template.py          # Copy this to write a new skill — one file, drop in, done
├── actions/                  # Bundled skills — each self-describes via a TOOL dict + handler
│   ├── mission.py            # Mission Control — what the session did, live
│   ├── task_agent.py         # Agentic multi-step tasks (plan → execute → report)
│   ├── rules.py              # Automation rules — time/file/phrase triggers
│   ├── macro.py              # Screen macro record/replay (confirm-gated)
│   ├── scrape.py             # URL → text/links/title (read-only)
│   ├── diagram.py            # Text spec → SVG flowchart/sequence/mindmap/timeline
│   ├── charts.py             # label=value → SVG bar/line/pie (no matplotlib)
│   ├── clip_history.py       # Clipboard history with search/use
│   ├── procman.py            # Process list/kill (confirm-gated, no self-kill)
│   ├── focus.py              # Pomodoro focus sessions (mutes proactive)
│   ├── window_layout.py      # Window tiling: split/coding/stack/left/right/center
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen & webcam capture for vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power (per-OS)
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher (validated names, no shell)
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations (undo journal)
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data (Open-Meteo, no key)
│   ├── video_player.py       # Plays video on the HUD, where the avatar normally is
│   ├── desktop.py            # Desktop and taskbar control (no code generation)
│   ├── scanner.py            # System / network / port / file inspection (read-only)
│   ├── flight_finder.py      # Flight search and extraction
│   ├── youtube_video.py      # YouTube transcript & playback helpers
│   ├── code_helper.py        # Screen + file code explanation and fixing
│   ├── dev_agent.py          # Multi-file project builder (allowlisted runner)
│   ├── game_updater.py       # Game/platform update helpers
│   ├── terminal.py           # Sandboxed terminal + git assistant (argv-only, repo-confined)
│   ├── vault.py              # AES-256-GCM password vault (master passphrase)
│   ├── rag.py                # Local RAG — index folders, ask offline (sqlite FTS5)
│   ├── history_search.py     # Conversation history search (local FTS5, survives restarts)
│   ├── mcp.py                # MCP client — tools from any stdio MCP server
│   ├── data_query.py         # DuckDB SQL over CSV/Parquet/JSON (offline)
│   ├── make_3d.py            # Spec → STL/OBJ mesh, preview on the 3D surface
│   ├── privacy.py            # Privacy mode — hard gate for cloud-facing tools
│   ├── multi_agent.py        # Planner→coder→tester dry-run pipeline (diffs before apply)
│   ├── presence.py           # Present/away detection — gates proactive speech
│   ├── smart_home.py         # MQTT pub/sub — HA / zigbee2mqtt / Tasmota / ESPHome
│   └── code_outline.py       # tree-sitter symbol outline (AST / stdlib / approximate)
├── dashboard/
│   ├── server.py             # Phone remote — FastAPI, PIN login, AES-256 command channel
│   └── static/               # login.html, app.html, vendored crypto-js
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   ├── semantic_recall.py    # Hybrid ranker for recall_memory (trigram + lexical)
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── long_term.json        # Persistent store — created on first run
├── core/
│   ├── gemini.py             # One place for every one-shot Gemini call — model ladder, timeouts, cooldowns
│   ├── llm_client.py         # Local LLM stack — Ollama is the DEFAULT (llama3.2); OpenAI-compatible aliases included
│   ├── prompt.txt            # All prompt wording — {tokens} are filled from the live system at startup
│   ├── avatar.py             # Avatar renderer — lighting, pose, expression, mouth (QPainter)
│   ├── avatar_mesh.py        # Head geometry — loads the face, generates skull/neck/rigs
│   ├── face_model.obj        # The face itself (MediaPipe canonical model, Apache-2.0, 25 KB)
│   ├── viseme.py             # Transcript → mouth shapes, fused with the audio's timing
│   ├── echo.py               # Tells your voice from the assistant's own echo; self-calibrating
│   ├── hotkey.py             # Push-to-talk chord — global on Windows, windowed fallback elsewhere
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   ├── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
│   ├── display.py            # Qt-free content panel renderer (unit-tested)
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── action_loader.py      # Bundled-action engine — the built-in twin of plugin_loader
│   ├── orchestrator.py       # Agent loop — plan → execute with stop-on-failure + report
│   ├── activity.py           # Mission Control timeline — every tool call, live
│   ├── events.py             # Event bus — file/time/USB/phrase events drive rules + proactive
│   ├── taskstore.py          # sqlite persistence for agentic runs (resume after restart)
│   ├── privacy.py            # Privacy mode — hard gate for cloud-facing tools
│   ├── presence.py           # Present/away tracker — gates proactive speech
│   └── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
└── config/
    ├── api_keys.json         # API key, name, voice, colour, toggles — created on first launch (git-ignored)
    └── certs/                # Self-signed TLS pair for the phone dashboard — generated locally (git-ignored)
```

---

## 🙏 Third-Party Assets

| Asset | Source | Licence |
| --- | --- | --- |
| `core/face_model.obj` | [MediaPipe](https://github.com/google-ai-edge/mediapipe) canonical face model — 468 vertices of measured human face geometry | Apache License 2.0 |

---

## 🔒 Your Data

Everything stays on your machine. There is no MARK server, no telemetry and no account.

| What | Where | Notes |
|---|---|---|
| Gemini API key, plugin credentials | `config/api_keys.json` | **Plaintext.** Anyone with your user account can read it. Treat it like a password file. |
| Dashboard TLS certificate + private key | `config/certs/` | Generated locally, self-signed, never leaves the machine. |
| What the assistant remembers about you | `memory/long_term.json` | Delete the file to make it forget everything. |

All three are listed in `.gitignore`, so a fork or a pull request cannot leak them by accident. **If you have already committed `config/api_keys.json` anywhere public, revoke that key** at [aistudio.google.com](https://aistudio.google.com/app/apikey) and generate a new one — removing the file in a later commit does not remove it from the history.

Your voice is streamed to Google's Gemini Live API while a session is open; that is the one thing that leaves your computer, and it stops when you mute or close the app.

---

## ⚠️ Known Limitations

Things worth knowing before you rely on them. Each one is a deliberate, documented trade-off rather than a bug — but none of them are obvious from the feature list.

**WhatsApp messages are sent blind.** `send_message` drives WhatsApp by typing into the app — it opens the window, searches the contact, types the message and presses Enter **without reading back which conversation received it**. On a slow machine the keystrokes can land in the wrong chat. There is a safe path in `actions/send_message.py` that verifies the window and the recipient, but it needs a driver that does not ship with JARVIS; drop a `plugins/_whatsapp_core.py` exposing `get()` and it takes over automatically. Until then, treat a WhatsApp send as unverified. Telegram, Signal and the rest go through the same keystroke path.

**Remote dashboard sessions expire.** A phone paired by QR gets a 12-hour session and can re-pair itself for 30 days, after which it must scan a new code. "Revoke devices" now kills the live bearer tokens *and* the pairing, so a revoked phone stops working immediately instead of at the next restart. (It did not, before this branch — it cleared the pairing only, so the phone in the room kept its access while the UI reported success.)

**Every skill is declared on every connection.** 76 tool declarations (~18,000 tokens) travel with each session whether you use them or not. Run `python tools/count_tools.py` to see the current cost. Tiering these is the highest-value change still outstanding.

**The GUI and the audio path have no automated tests.** `tools/feature_audit.py` proves the tool registry and the platform integrations run in *this* environment, and CI proves the logic layer — but nothing exercises the PyQt HUD, the TTS/STT pipeline or the wake word end to end. Run `python tools/feature_audit.py` after an upgrade rather than assuming.

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| YouTube | [@ankitmeena](https://www.youtube.com/@ankitmeena) |
| Instagram | [@ankitmeena](https://www.instagram.com/ankitmeena) |

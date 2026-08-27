"""Home-automation-helper personality customization.

Applied on top of ZeroClaw's own default personality templates
(`GET /api/personality/templates`) when creating a new agent through this
integration's config flow — not a replacement for them. AGENTS.md,
HEARTBEAT.md and MEMORY.md are operational, not identity-specific, and are
written back completely unchanged; SOUL.md and IDENTITY.md get a
home-assistant-helper role layered on, USER.md gets one extra section
inviting the user to list who lives in the house, and TOOLS.md gets a
notify-webhook section appended when a notify URL was configured (see
`webhook.py`) — otherwise left untouched like the other operational files.
Nothing here invents real household member names or other personal
details — this integration has no access to that information; it just
leaves the right place for the user to fill in themselves (in ZeroClaw's
own dashboard, or by hand).

Kept deliberately concise: this text is read by the model at the start of
every session (per AGENTS.md's own instructions), so shorter is both
cheaper and more reliably followed than a heavily-annotated version — the
reasoning behind each rule belongs in docs/DECISIONS.md, not here.

Language: `hass.config.language` picks which translation of this text gets
written. Two languages have a full, hand-written translation of the actual
content (`en`, `it` — see `_SOUL_ADDITIONS` etc.); every *other* language
Home Assistant itself supports (`_HA_LANGUAGE_NAMES`, the exact 64-code set
from `homeassistant/generated/languages.py`) still gets handled, not just
silently defaulted to English text — it gets the English content with the
"Always respond in English." directive swapped for that language's own
native name (`_localize`). That's a deliberate scope decision, not
laziness: hand-translating multi-paragraph content into all 64 languages
would mean shipping translations in scripts and languages this project has
no way to actually verify the quality or even correctness of (Thai, Telugu,
Welsh, ...) — the directive-swap approach is something every mainstream LLM
follows reliably regardless of the instruction's own language, so it
delivers the thing that was actually asked for (the agent responds in the
household's language) without the honesty problem of claiming translation
quality nobody checked. `en`/`it` get the fuller, natural treatment because
they're this integration's own primary languages.

Only the additions this module writes are affected either way — ZeroClaw's
own base template content (fetched fresh from `/api/personality/templates`)
is left exactly as `/api/personality/templates` returns it, whatever
language that happens to be; the language directive is what makes the
agent's actual replies follow the household's language regardless.
"""

from __future__ import annotations

import re
import unicodedata

# The exact language codes Home Assistant's own frontend supports, each
# mapped to its native display name — confirmed against
# `homeassistant/generated/languages.py` (checked 2026-08-27; regenerated
# from the frontend's own translations directory, so this is the same list
# `hass.config.language` is validated against). Used both to decide a code
# is real (as opposed to malformed input) and to name the language in the
# fallback directive for anything without a full translation below.
_HA_LANGUAGE_NAMES: dict[str, str] = {
    "af": "Afrikaans", "ar": "العربية", "bg": "Български", "bn": "বাংলা",
    "bs": "Bosanski", "ca": "Català", "cs": "Čeština", "cy": "Cymraeg",
    "da": "Dansk", "de": "Deutsch", "el": "Ελληνικά", "en": "English",
    "en-GB": "English", "eo": "Esperanto", "es": "Español",
    "es-419": "Español", "et": "Eesti", "eu": "Euskara", "fa": "فارسی",
    "fi": "Suomi", "fr": "Français", "fy": "Frysk", "ga": "Gaeilge",
    "gl": "Galego", "gsw": "Schwiizerdütsch", "he": "עברית", "hi": "हिन्दी",
    "hr": "Hrvatski", "hu": "Magyar", "hy": "Հայերեն",
    "id": "Bahasa Indonesia", "is": "Íslenska", "it": "Italiano",
    "ja": "日本語", "ka": "ქართული", "ko": "한국어",
    "lb": "Lëtzebuergesch", "lt": "Lietuvių", "lv": "Latviešu",
    "mk": "Македонски", "ml": "മലയാളം", "nb": "Norsk bokmål",
    "nl": "Nederlands", "nn": "Norsk nynorsk", "pl": "Polski",
    "pt": "Português", "pt-BR": "Português", "ro": "Română", "ru": "Русский",
    "sk": "Slovenčina", "sl": "Slovenščina", "sq": "Shqip", "sr": "Српски",
    "sr-Latn": "Srpski", "sv": "Svenska", "ta": "தமிழ்", "te": "తెలుగు",
    "th": "ไทย", "tr": "Türkçe", "uk": "Українська", "ur": "اردو",
    "vi": "Tiếng Việt", "zh-Hans": "简体中文", "zh-Hant": "繁體中文",
}

# The English directive sentence every fallback translation swaps out for
# the target language's own native name — must match _SYSTEM_PROMPT_
# TEMPLATES["en"] and _SOUL_ADDITIONS["en"] verbatim (a unit test would
# normally guard this; kept as one literal here since it's two call sites).
_EN_DIRECTIVE = "Always respond in English."


def _resolve_ha_language(language: str | None) -> str:
    """Map an HA language tag (`hass.config.language`, e.g. `it`, `en-GB`,
    `zh-Hans`) to itself if it's a real HA language code, else to its
    lowercased base subtag if *that's* a real code (defensive, in case a
    caller passes something like `de-AT` that HA itself wouldn't), else to
    `"en"`. Does not restrict to the two fully-translated languages — see
    `_localize` for how a not-fully-translated-but-real code is handled."""
    if not language:
        return "en"
    if language in _HA_LANGUAGE_NAMES:
        return language
    base = language.split("-")[0].lower()
    if base in _HA_LANGUAGE_NAMES:
        return base
    return "en"


def _localize(translations: dict[str, str], lang: str) -> str:
    """Return `translations[lang]` if a full translation exists; otherwise
    the English entry with its response-language directive swapped for
    `lang`'s own native name (see module docstring for why)."""
    if lang in translations:
        return translations[lang]
    name = _HA_LANGUAGE_NAMES.get(lang, "English")
    return translations["en"].replace(
        _EN_DIRECTIVE, f"Always respond only in {name}."
    )


_SOUL_ADDITIONS: dict[str, str] = {
    "en": """

## Role: Home Assistant Helper

Always respond in English. Your focused job on top of everything above:
help around this home through Home Assistant.

- Be warm, patient, and respectful — this is someone's home, not a dev tool.
- Know the household from `USER.md`; use names naturally.
- A named room or place is a Home Assistant **Area** — target it via the
  `area` argument, don't search for an entity with that exact name. If the
  wording isn't a literal area name, check `GetLiveContext` for the real
  areas/aliases and try the obvious match before asking to clarify.
- A plural entity type ("the lights", "the switches") is a `domain`
  filter, combinable with `area`, not one entity: lights→light,
  switches/outlets→switch, blinds/shades/curtains/garage doors/gates→cover,
  thermostats/AC→climate, fans→fan, speakers→media_player, locks→lock,
  vacuums→vacuum, humidifiers→humidifier.
- If area/domain resolves to one entity, or you already found it earlier
  this conversation, act — don't ask for an exact name. Resolve "it"/
  "them"/"that one" to the same target as the previous turn.
- Build a lasting mental map of the house: `memory_store` area
  names/aliases/domains/entity names as you learn them; check
  `memory_recall` before re-discovering via `GetLiveContext`. Live state
  (on/off, temperature) still needs a fresh check.
- Prefer acting over asking for reversible actions (lights, thermostat,
  sensors). Ask first for anything higher-stakes.
- Covers (gates, garage doors, blinds/windows) are security-sensitive: use
  `HassOpenCover`/`HassCloseCover`/`HassSetPosition`/`HassStopMoving`
  specifically, never the generic `HassTurnOn`/`HassTurnOff`/`HassToggle`
  on them — the generic ones skip the confirmation the dedicated tools
  trigger.
- Locks have no dedicated tool (`HassTurnOn`=lock, `HassTurnOff`=unlock,
  same tools as lights) — always ask before touching one, no exception.
- Confirm plainly after acting ("Turned off the kitchen lights"), no
  technical detail.
- If you don't have control over something, say so honestly.
- After finishing something the household didn't watch you do in this
  conversation — a scheduled (cron) task, or a task you started because a
  Home Assistant automation told you about an event — notify them (see
  `TOOLS.md` for how). Don't notify for ordinary requests made directly in
  this conversation; they already have your reply for those.
- **"Tell me when X happens" is a watch to create (see `TOOLS.md`), not a
  promise to check later — and it means once, not forever, unless they say
  otherwise.** "Let me know when the washing machine finishes" is one
  event, not a standing rule; only arm a recurring watch when the household
  actually says "every time" or gives a real recurring schedule. When they
  add a follow-up action ("...then start the dryer"), that instruction is
  what you send yourself as the watch's message, not something to remember
  separately.
""",
    "it": """

## Ruolo: Assistente per la Casa

Rispondi sempre in italiano. Il tuo compito specifico, oltre a tutto quanto
sopra: aiutare in questa casa tramite Home Assistant.

- Sii caloroso, paziente e rispettoso — questa è una casa, non uno
  strumento di sviluppo.
- Conosci chi vive qui da `USER.md`; usa i nomi delle persone naturalmente.
- Una stanza o un luogo nominato è un'**Area** di Home Assistant —
  targetizzala tramite l'argomento `area`, non cercare un'entità con quel
  nome esatto. Se il termine usato non è il nome letterale di un'area,
  controlla `GetLiveContext` per le aree/alias reali e prova la
  corrispondenza ovvia prima di chiedere chiarimenti.
- Un tipo di entità al plurale ("le luci", "gli switch") è un filtro
  `domain`, combinabile con `area`, non una singola entità:
  luci→light, prese/interruttori→switch, tapparelle/tende→cover,
  termosifoni/clima/condizionatori→climate, ventilatori→fan,
  diffusori/altoparlanti→media_player, serrature→lock, aspirapolvere→vacuum,
  umidificatori→humidifier.
- Se area/domain individua una sola entità, o l'hai già trovata prima in
  questa conversazione, agisci — non chiedere il nome esatto. Risolvi
  "quella"/"accendile" e riferimenti simili sullo stesso bersaglio del
  turno precedente.
- Costruisci una mappa duratura della casa: usa `memory_store` per salvare
  nomi/alias delle aree, domini presenti, nomi delle entità man mano che li
  scopri; controlla `memory_recall` prima di riscoprire tutto con
  `GetLiveContext`. Lo stato in tempo reale (acceso/spento, temperatura) va
  comunque sempre riverificato.
- Preferisci agire piuttosto che chiedere per azioni reversibili (luci,
  termostato, sensori). Chiedi prima solo per cose più delicate.
- Le coperture (cancelli, porte del garage, tapparelle/finestre) sono
  sensibili per la sicurezza: usa specificamente
  `HassOpenCover`/`HassCloseCover`/`HassSetPosition`/`HassStopMoving`, mai i
  tool generici `HassTurnOn`/`HassTurnOff`/`HassToggle` su di esse — quelli
  generici saltano la conferma che i tool dedicati attivano.
- Le serrature non hanno un tool dedicato (`HassTurnOn`=blocca,
  `HassTurnOff`=sblocca, stessi tool delle luci) — chiedi sempre conferma
  prima di toccarne una, senza eccezioni.
- Conferma in modo semplice dopo aver agito ("Ho spento le luci in
  cucina"), niente dettagli tecnici.
- Se non hai il controllo su qualcosa, dillo onestamente.
- Dopo aver portato a termine qualcosa che la famiglia non ti ha visto fare
  in questa conversazione — un task schedulato (cron), o un task avviato
  perché un'automazione di Home Assistant ti ha segnalato un evento —
  avvisali (vedi `TOOLS.md` per come farlo). Non avvisare per le richieste
  ordinarie fatte direttamente in conversazione: per quelle hanno già la
  tua risposta.
- **"Dimmi quando succede X" è un watch da creare (vedi `TOOLS.md`), non
  una promessa di controllare più tardi — e significa una volta, non per
  sempre, a meno che non dicano altrimenti.** "Fammi sapere quando finisce
  la lavatrice" è un evento singolo, non una regola permanente; arma un
  watch ricorrente solo quando la famiglia dice esplicitamente "ogni volta"
  o dà una vera ricorrenza. Quando aggiungono un'azione successiva ("...poi
  avvia l'asciugatrice"), quella istruzione è ciò che ti invii come
  messaggio del watch, non qualcosa da ricordare separatamente.
""",
}

_USER_ADDITIONS: dict[str, str] = {
    "en": """

## Who Lives Here

(Add the people in this household here — names, and anything worth knowing
about how each person likes things done. This is read every session.)
""",
    "it": """

## Chi Vive Qui

(Aggiungi qui i membri della famiglia — nomi e qualsiasi cosa utile su come
ciascuno preferisce che le cose vengano fatte. Letto ad ogni sessione.)
""",
}

_TOOLS_ADDITIONS: dict[str, str] = {
    "en": """

## Notifying the Household, and Watching for Events

One endpoint, `POST {url}` (your `http_request` tool — no auth header
needed, the URL itself is the credential), for two related things. Every
call is JSON, dispatched on `"type"`.

**Notify** — send the household a notification (shown in Home Assistant,
pushed to their phone if they've set that up), for anything worth telling
them outside of a live conversation (see SOUL.md for exactly when):
`{{"type": "notify", "message": "<what happened, in plain language>"}}`.

**Watch for a state change** — the event-driven alternative to checking
repeatedly: `{{"type": "create_watch", "entity_id": "<the entity>",
"to_state": "<the state that means it happened>", "message": "<what to
tell yourself when it fires>", "recurring": false}}`. When the entity
reaches that state, `message` comes back to you as a fresh instruction —
act on it with your normal tools, same as anything else you're told.

**`"recurring"` defaults to `false` — a watch fires once and then
disarms itself.** This matters: if the household says "tell me when the
washing machine finishes" with no mention of "every time" or a recurring
schedule, that means *once*, not forever — leave `recurring` false (or
omit it). Only set `true` when they actually ask for it every time this
keeps happening.

Manage what's armed with `{{"type": "list_watches"}}` (returns every watch
you currently have armed, so you can check before creating a duplicate or
answer "what are you watching for me?" truthfully) and
`{{"type": "cancel_watch", "watch_id": "<id>"}}` (from `create_watch`'s or
`list_watches`'s response) to disarm one early, e.g. if asked to stop.

If any of these calls fail, mention it plainly next time someone talks to
you rather than retrying silently in a loop.
""",
    "it": """

## Avvisare la Famiglia, e Osservare Eventi

Un solo endpoint, `POST {url}` (il tuo tool `http_request` — non serve
nessun header di autenticazione, l'URL stesso fa da credenziale), per due
cose collegate. Ogni chiamata è JSON, smistata sul campo `"type"`.

**Avvisare** — invia alla famiglia una notifica (visibile in Home
Assistant, e sul telefono se lo hanno configurato), per qualsiasi cosa
valga la pena comunicare al di fuori di una conversazione dal vivo (vedi
SOUL.md per capire esattamente quando):
`{{"type": "notify", "message": "<cosa è successo, in linguaggio semplice>"}}`.

**Osservare un cambio di stato** — l'alternativa event-driven al
controllare ripetutamente: `{{"type": "create_watch", "entity_id": "<l'entità>",
"to_state": "<lo stato che significa che è successo>", "message": "<cosa
dirti quando scatta>", "recurring": false}}`. Quando l'entità raggiunge
quello stato, `message` ti torna indietro come un'istruzione nuova — agisci
con i tuoi tool normali, come per qualsiasi altra cosa ti venga detta.

**`"recurring"` di default è `false` — un watch scatta una volta e poi si
disattiva da solo.** Questo è importante: se la famiglia dice "avvisami
quando finisce la lavatrice" senza menzionare "ogni volta" o una ricorrenza,
significa *una volta*, non per sempre — lascia `recurring` a false (o
omettilo). Metti `true` solo quando chiedono esplicitamente che avvenga
ogni volta che si ripresenta.

Gestisci cosa è attivo con `{{"type": "list_watches"}}` (restituisce tutti i
watch che hai attualmente armati, così puoi controllare prima di crearne
uno duplicato, o rispondere onestamente a "cosa stai osservando per me?") e
`{{"type": "cancel_watch", "watch_id": "<id>"}}` (dalla risposta di
`create_watch` o `list_watches`) per disattivarne uno prima del previsto,
es. se ti viene chiesto di fermarlo.

Se una di queste chiamate fallisce, menzionalo semplicemente la prossima
volta che qualcuno ti parla, invece di ritentare silenziosamente in un
ciclo.
""",
}

_IDENTITY_VIBE: dict[str, str] = {
    "en": "Warm, patient, and respectful — a helpful presence around the house, not a corporate assistant.",
    "it": "Calorosa, paziente e rispettosa — una presenza utile in casa, non un assistente aziendale.",
}

_SYSTEM_PROMPT_TEMPLATES: dict[str, str] = {
    "en": (
        "You are {name}, a warm and respectful home assistant helper. "
        "You help the people who live here with home automation tasks through "
        "Home Assistant, and you know who they are. Read SOUL.md, IDENTITY.md, "
        "and USER.md at the start of every session. Always respond in English."
    ),
    "it": (
        "Sei {name}, un assistente domestico caloroso e rispettoso. Aiuti le "
        "persone che vivono qui con l'automazione della casa tramite Home "
        "Assistant, e sai chi sono. Leggi SOUL.md, IDENTITY.md e USER.md "
        "all'inizio di ogni sessione. Rispondi sempre in italiano."
    ),
}


def default_system_prompt(display_name: str, language: str | None = None) -> str:
    """Short base system prompt for a newly created home-helper agent, in
    the language `hass.config.language` resolves to (see
    `_resolve_ha_language`/`_localize` — every HA-supported language is
    handled, not just the two with a full hand-written translation).

    `display_name` is what the user actually typed (not necessarily the
    same as the sanitized ZeroClaw alias — see `sanitize_agent_alias`),
    since this text is only ever read by the model / shown to the household,
    never used as an identifier.

    Deliberately brief — the real depth lives in the personality files
    (SOUL.md especially), which ZeroClaw's own agent loop reads at the start
    of every session per AGENTS.md's instructions. This is just what the
    model sees before that.
    """
    lang = _resolve_ha_language(language)
    template = _localize(_SYSTEM_PROMPT_TEMPLATES, lang)
    return template.format(name=display_name)


def build_personality_files(
    templates: list[dict],
    display_name: str,
    language: str | None = None,
    notify_webhook_url: str | None = None,
) -> list[dict]:
    """Return `[{"filename", "content"}, ...]` ready to write: ZeroClaw's own
    default templates, with the home-helper role layered onto SOUL.md,
    IDENTITY.md, and USER.md, in the language `hass.config.language`
    resolves to (see `_resolve_ha_language`/`_localize` — every
    HA-supported language is handled, not just the two with a full
    hand-written translation). `templates` is `GET /api/personality/
    templates`'s `"files"` list — passed through unchanged for any
    filename this doesn't specifically customize. `display_name` is what
    the user actually typed (see `default_system_prompt`) — used for
    IDENTITY.md's `Name:` line. `notify_webhook_url` is the full URL
    (`<ha_url>/api/webhook/<id>`, see `webhook.py`) an agent's
    `http_request` tool can POST a `{"message": ...}` body to for a
    household notification — appended to `TOOLS.md` when given; `TOOLS.md`
    is left untouched (same as every other filename this doesn't
    specifically customize) when `None`, e.g. no HA URL was configured
    during setup.
    """
    lang = _resolve_ha_language(language)
    identity_overrides = {
        "name": display_name,
        "vibe": _localize(_IDENTITY_VIBE, lang),
        "emoji": "🏠",
    }

    out = []
    for f in templates:
        filename = f.get("filename", "")
        content = f.get("content", "")

        if filename == "SOUL.md":
            content = content.rstrip("\n") + "\n" + _localize(_SOUL_ADDITIONS, lang)

        elif filename == "IDENTITY.md":
            for label, value in identity_overrides.items():
                # Templates use "- **Label:** value" lines (case-insensitive
                # label match, keeps ZeroClaw's own markdown formatting).
                lines = content.split("\n")
                marker = f"**{label.capitalize()}:**"
                for i, line in enumerate(lines):
                    if marker.lower() in line.lower():
                        lines[i] = f"- {marker} {value}"
                        break
                content = "\n".join(lines)

        elif filename == "USER.md":
            content = content.rstrip("\n") + "\n" + _localize(_USER_ADDITIONS, lang)

        elif filename == "TOOLS.md" and notify_webhook_url:
            addition = _localize(_TOOLS_ADDITIONS, lang).format(url=notify_webhook_url)
            content = content.rstrip("\n") + "\n" + addition

        out.append({"filename": filename, "content": content})

    return out


def sanitize_agent_alias(raw_name: str) -> str:
    """Turn free-typed text into a ZeroClaw-valid agent alias.

    ZeroClaw's own validation (confirmed against a real running gateway by
    reading `POST /api/quickstart/apply`'s rejection messages one at a
    time, undocumented anywhere): lowercase ASCII letters and digits only,
    single underscores as separators, must start AND end with a
    letter/digit, no `__` (reserved as the env-var grammar's path
    separator), no unicode at all (rejected at the JSON-body level, before
    ZeroClaw's own validation even runs).

    Returns `""` if nothing usable survives sanitization (e.g. the input
    was only symbols/whitespace) — callers should treat that as invalid
    input, not silently fall back to something.
    """
    # Fold accented Latin letters to their ASCII base (e.g. "café" -> "cafe")
    # rather than just dropping them — friendlier for names in most
    # European languages than a plain "keep only [a-z0-9]" pass would be.
    ascii_text = (
        unicodedata.normalize("NFKD", raw_name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    lowered = ascii_text.lower()
    # Any run of one-or-more disallowed characters becomes exactly one
    # underscore, so "casa mia!!" and "casa---mia" both collapse to
    # "casa_mia" rather than leaving doubled/trailing separators.
    collapsed = re.sub(r"[^a-z0-9]+", "_", lowered)
    return collapsed.strip("_")

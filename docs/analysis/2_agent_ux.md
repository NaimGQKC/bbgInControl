# Sparrow AI: agent-side UX review (lens 2)

Evidence: code (`app/web_templates/onboarding.html` = OB, `app/onboarding.py`, `app/engine.py`, `app/templates.py`, `app/admin.py`), QA screenshots/JSON in `scratchpad/qa/`, and my own Playwright probes on a local fake-mode copy (`scratchpad/ux/`). Fake mode cannot show real Meta/Google behaviour, so Meta-on-phone claims are flagged as unverified.

## Verdict
The chat is well built and the "proof" moments (calendar read, real test event in their Google Calendar, exact first-message preview) are the best part. The weak points are not the chat but (a) the Meta step on a phone, (b) the silent wait after it, and (c) everything that happens when something fails, which is mostly invisible to both agent and operator.

## Findings

### HIGH

**H1. Reduced-motion users see no assistant text at all (bug I reproduced).**
OB:26 `.w{opacity:0; animation:... forwards}` and OB:96 `@media (prefers-reduced-motion){*{animation:none!important}}`. With animation off the words stay at opacity 0. Probe: `reducedMotion:'reduce'` gives word opacity `0,0,0,0,0,0`; screenshot `scratchpad/ux/rm-reduce.png` shows only "Hola, soy Lucía" and a Google button, no body text. Also affects Meta coaching lines. Fix: in the reduce block set `.w{opacity:1!important; filter:none!important; transform:none!important}` or skip the word-span path when `calm` is true in `add()`.

**H2. Abandoned onboarding is never chased, and the operator never hears about it.**
`AGENT_ONBOARDING_NUDGE` exists (templates.py:163) but is sent nowhere (grep: only defined). The link (`/o/<token>`) is the only credential and is not stored anywhere for the agent: no "save this link" line, the email/WhatsApp copy is only offered on phones at the Meta step via a `mailto:` (OB:~656, fails if no mail app). Fix: (1) as soon as Google gives the email, email the link ("Continúa donde lo dejaste") and, once the mobile is known, WhatsApp the nudge template after ~2 h and ~24 h idle at the same step; (2) daily ops digest "stuck at step X for N h" (the dashboard only lists the step, admin_dashboard.html:36).

**H3. First-contact template rejected, or never approved: the agent page lies and the operator is not told.**
onboarding.py:336-339: REJECTED sends only `alert_agent` ("lo revisamos nosotros") plus an event; there is no `alert_ops` (the cold template does alert ops, line 218). The page keeps saying "Solo falta que WhatsApp apruebe mis mensajes" (OB:686) forever. After 3 days of pending the check job just stops (onboarding.py:342) with no alert. Also only `LEAD_FIRST_CONTACT` gates go-live; follow-ups, confirmation and reminders can still be pending/rejected while the account is "live". Fix: `alert_ops` on rejection and on pending > 24 h; page text for a rejected state ("Hay un problema con un mensaje; lo arreglamos nosotros, no tienes que hacer nada"); go-live checks all six templates or lists which are missing.

**H4. Meta payment method is told but never verified (the 131042 case).**
OB:~658 warns twice, but `connect_meta` (onboarding.py) never checks billing, and `wa_check` is a self-declared tap (OB:616, `/step`). The failure only shows up on the first real lead as `cause_payment` (lang.py:137), delivered through the "lead needs you" template, and the operator is not alerted (`_delivery_failed` has no `alert_ops`). Fix: after connect, query the WABA's payment/funding state (I believe `primary_funding_id` on the WABA node; verify in Meta docs) and, if absent, hold the page on a blocking step with the exact path ("WhatsApp Manager → Métodos de pago", plus a re-check button) instead of "live". Also `alert_ops` on the first 131042 per account. Do not flip to "listo 🎉" until the check passes.

**H5. Meta step on a phone is the likely biggest drop-off and untested.**
Most agents will tap the link from WhatsApp/email on a phone. The chat itself admits "mucho más fácil desde un ordenador, porque tienes que escanear un código" (OB:~653): you cannot scan a QR that is on the same phone's screen. The fallback is a `mailto:` link. QA ran fake mode only (`FAKE` branch in `launchSignup`, OB:~1012), so the real phone path is not evidenced. Fix: test the real flow on iOS/Android once (Meta may offer an in-app deep link instead of QR for Coexistence; verify). If a computer is required: make "continue on computer" a first-class branch the moment the user lands on a phone (send the link via WhatsApp to their mobile, show a 6-character code, "ábrelo en el ordenador"), and defer the Meta step instead of presenting it as "último paso" in the same breath as the other steps. Meanwhile ask everything else on the phone and park the agent at "Falta un paso en el ordenador".

**H6. No terms/privacy acceptance and no legal links anywhere in the onboarding.**
`grep terminos|privacidad onboarding.html` returns nothing. The agent grants Google Calendar access, connects their WhatsApp, and (prospecting) accepts legal responsibility for cold messages, without ever seeing or clicking the DPA/terms (CLAUDE.md pending item 0 admits it). Fix: one line with links under the first Google button ("Al continuar aceptas los Términos y la Privacidad") plus a stored `terms_accepted_at`; footer links (the landing has them, the chat doesn't).

### MED

**M1. "Lo dejamos listo en 10 minutes" (OB:451, hero, all 3 languages) is not true.** Realistic: ~14 agent inputs + Facebook login/2FA + QR + card + a Meta approval wait of "horas, a veces un día" (OB:686). Say "10 minutos de tu parte; WhatsApp tarda unas horas en aprobar". Overpromise then wait = the agent assumes it is broken.

**M2. Same number is called two things.** OB:~560 asks "tu móvil de WhatsApp… desde el que escribiré a tus propietarios y al que te aviso" (sender = alert target), then OB:~679 asks again "¿te va bien ese número?" for the alert. Many agents have a business line and a personal mobile. Fix: ask once, as "¿Dónde te aviso?" with a default of the connected number, and tell them the alert will come from a different number called "Sparrow" (save it as a contact). When alert number == sender number the chat should say so ("te llegará en tu mismo WhatsApp Business, en un chat aparte").

**M3. Alert copy is wrong for undelivered messages and thin for handoffs.** `AGENT_NEEDS_YOU` (templates.py:124) always ends "Ábrelo en tu WhatsApp Business", but for `why_delivery_failed` ("…Escríbele o llámale tú") the message never reached the owner, so the chat may not exist. Split in two templates: "te necesita" (chat) vs "no he podido entregar; llámale: +34…". Additional gaps: no deep link (a CTA URL button `https://wa.me/<lead>` opens the right chat in the Business app; today they must hunt for it), no statement that the assistant has stepped aside (G8) and how to hand back ("Sigue con Marta" is only taught if they message the Sparrow number something else, `resume_help`, engine.py:707), no urgency, no deadline. FR `cause_payment` (lang.py:137) uses tu/ton while all FR templates use vous.

**M4. Post-live is a black box.** Final message: "No hace falta volver aquí" (OB:691). Missing: a first-lead confirmation ("Primer lead contactado: Marta, hace 12 s"), per-booking WhatsApp (pending item 5), a weekly one-line digest ("Esta semana: 9 leads, 4 respuestas, 2 citas"), and an email backup channel (the email is known from Google; WhatsApp alerts fail silently: `alert_failed`/`alert_not_sent` events are not shown anywhere in /admin). Without any of this the agent cannot tell "working" from "dead", and the billing per booked meeting (€30) arrives without any visible record. No dashboard is fine; a digest is not a dashboard.

**M5. Failure recovery is mostly generic.**
- Meta popup closed for any reason gives one text: "La ventana se cerró antes de terminar" (OB:~1031); `CANCEL`/`FINISH_ERROR` events are logged but not turned into specific help (wrong account type, number already on API, number not on Business app). Fix: map `data.event`/`current_step` to a message and offer "Hablar con una persona" prefilled with the last Meta step.
- Wrong account type is detected only by self-declaration ("Uso WhatsApp Business" works even if untrue). Mitigate: after connect failure with a known error code show the "descarga WhatsApp Business" guide again.
- Google denied: copy says "no se completó el permiso" but not why/how (granular consent: check every box); popup path and redirect path differ.
- Bad link shows raw JSON `{"detail":"Enlace no válido"}` (confirmed with curl; same for OAuth callback `state inválido`). Replace with a friendly page "Este enlace ha caducado; empieza aquí" plus the help button.
- Test calendar event stays in their calendar if they abandon before tapping "borrar la prueba" (OB:~536 `go()`); it also blocks a real slot.

**M6. Step order front-loads low-value taps.** See proposed order below. Candidates to cut or defer: name confirmation ("¿Te llamo Laura?", Google already gave it), lead-language confirmation when the screen is Spanish, free-text preferences (optional, can be asked after go-live), the website/Instagram question (optional; Meta has its own field; pre-fill from the email domain), the second alert-number question, the Stripe step if it is ever turned on (contradicts "no setup fee" in CLAUDE.md, the landing "solo pagas por cita" and the help-chat system prompt; OB:~600 says "{fee} € una sola vez").

**M7. Accessibility beyond H1.**
- Tap targets: "Hablar con una persona" 32 px high (probe: 32), composer send 42x42, attach 40x44, chip "Copiar" ~30 px, `.suggest` pills 6px padding. The language switch is OK at 44.
- Composer `<textarea id=ci>` has no label (probe: `aria-label` null); only a changing placeholder. Add `aria-label` and a visible-focus style: `outline:none` (OB:46) and the wrapper has no `:focus-within` ring.
- `#msgs aria-live="polite"` plus `render()` clearing and re-adding the whole transcript on every tap makes screen readers re-read the whole conversation. Use live region only for the newest message, or `aria-live=off` on replays.
- Contrast: accent `#B4553A` on page bg = 4.37:1 (fails 4.5 for the 13 px "Hablar con una persona", OB:34, and for links); `.who span` is 10.5 px uppercase mono; muted on bg 5.48 (pass). Make help button 15-16 px, darken accent text to about `#A14A31`.
- Language switch reloads the page and replays the chat with animation (known); also uses `location.href` so any typed draft is lost.
- Mic hidden on iOS (fine), highlight `.hl` is decoration only (fine).

**M8. Operator console (founder runs demos from it).**
- "Activar" (`go_live`, admin.py:~85) is one tap, no confirmation, no check that Meta is connected, templates approved, or payment method present; a mis-tap on a real agent starts leads into a non-working sender. Same for "Pausar". Add a confirm and show red preconditions next to the button.
- All times are UTC with the suffix "UTC" (admin_dashboard.html), the founder is in Madrid; demos get confusing around the 09:00-21:00 window. Show Europe/Madrid.
- Dashboard has no "stuck" view (step + hours since last activity), no template status per account, no alert-failure feed (`alert_failed`, `alert_not_sent`, `send_failed` with code), no 131042 flag. Help requests show only the last 10 with no "done" state.
- Not usable on a phone (QA: hscroll 4884>1440 dashboard, 2695>1440 account). Matters because alerts reach the founder on WhatsApp and he will want to act from there.
- Test lead: "Marcar como prueba" is on by default (good); the "any_hour" box is right under it (can send outside hours by mistake); creating a test lead for an existing test number silently deletes the old one and its chat (admin.py:~172). The raw "Enviar" form sends real WhatsApp text from the agent's number to any number that is not a lead.
- The onboarding URL (the agent's only credential) is displayed in plain text on the account page; fine for the operator, just never screen-share it during a demo.

### LOW
- **L1.** FR phone error text says "format 600 000 000" while the FR placeholder is "06 12 34 56 78" (web.py:176 hard-codes the Spanish format; i18n maps it literally). Make the server return the format for the lead language.
- **L2.** Spanish has 6 ready-made cities only (OB:~300); anywhere else the agent types (fine) but tapping a city then neighbourhoods adds taps for no benefit to someone in Alicante. Offer "Escribir otra zona" first.
- **L3.** `.opts.solo` full-width Google button is good; the secondary pills use `#E4DCCD` borders at about 1.3:1 against the background (1.4.11 non-text contrast).
- **L4.** The orb colour/progress ring changes carry meaning only visually; `1/4` progress label is below 12 px next to an uppercase mono subtitle.
- **L5.** Typing/typewriter pacing (`say()` sleeps up to 1 s per bot line) is charming once and slow on a re-visit for the agent who needs to fix something (replays are instant, fine) but a first-time user pays about 15-25 s of pure waiting; add tap-to-skip.
- **L6.** "Mis datos / Preguntar algo / Hablar con una persona" are three help affordances in one 3-chip header; the third is the one that matters (a human) but is the smallest.

## Direct answers

1. **Drop-off.** Highest risk, in order: Meta signup (Facebook login/2FA, QR on a phone, card) > the wait after it (hours/a day, no progress signal besides a promised WhatsApp) > the Google consent screen (a sensitive-scope calendar grant from a stranger's page, no legal links) > the long run of optional questions. Cards: asking is unavoidable only because Meta needs a payment method; make it a verified, explained step (H4), not a footnote. QR: only on desktop (H5). Business-app check: replace the quiz ("¿el icono tiene una B?") with Meta's own error handling, or keep it to one tap (the "No lo sé" subtree is three screens). Facebook business portfolio: do not mention it; Meta may force it, so wait for real-flow evidence. Alert number: merge into one question (M2).
2. **Named AI persona vs wizard.** The chat is a good fit for the first half: one question per screen, big tap targets, instant answers, and an honest "IA" label. It is a poor fit for the second half: step 4 contains a 10-screen foreign flow where a persistent checklist ("1 Google ✓, 2 Tus datos ✓, 3 WhatsApp, 4 Aprobación") with a visible state is more reassuring than a scrolling transcript; at present the header shows only "3/4" and in the final wait a card. Keep the chat voice but add a pinned step list on the waiting screen and a "volver a ver instrucciones" action. Trust is helped by the real test event; hurt by missing legal links (H6) and the unsupported "10 minutos" (M1). The pushy "Hablar con una persona" should be a 44 px primary action on a failure.
3. **Failures.** See M5/H3/H4. Self-recovery exists for Google (retry button), Meta closed (retry), wrong account type (guide), help chat. Not covered: rejected templates (operator-only, silent), missing payment, number already on the API, number not in Business app (surface as a generic "Algo falló"), 3-day approval timeout. The operator sees help requests and `meta_connect_failed`/`template_rejected` only as events inside each lead/account, not as a queue.
4. **After live.** WhatsApp alerts only on handoff/failed delivery, Google Calendar for bookings. Fix with M3/M4.
5. **Copy.** Spanish is warm and clear ("el asistente virtual de…" rule respected). Specific issues: mixed formal levels in FR (tu in `cause_payment`, vous elsewhere), "Il s'affiche autre chose" is awkward French (suggest "Autre chose s'affiche"), "Valoración agendada" test-event wording is correct but the preview shows a fake conversation as if real before saying "demostración" (the line is there, keep it above the bubble), `tr()` fallback shows Spanish when a key is missing (tests guard this). Overall tone is good for 45+: short, no jargon, one action per message.
6. **Operator console.** M8.

## Proposed ideal onboarding order (changes in bold)
1. Landing CTA (keep) with terms link and "10 minutos de tu parte".
2. Google (name + email from Google; **terms accepted here**; **email the resume link immediately**).
3. Calendar proof + test event preview (keep; delete test event automatically after 1 h).
4. Agency + zones (agency hint from email domain; skip name and language confirmations when screen = Spanish).
5. **Where to alert you** (one question, one default).
6. **Detect device: phone -> "Continúa en el ordenador" branch; desktop -> continue.**
7. WhatsApp Business: one tap ("¿Tiene una B?") merged with the Meta instructions; no website question.
8. Meta Embedded Signup; **verify payment method and WABA state automatically**; blocking recovery card if missing.
9. Waiting screen with a pinned checklist, **auto-WhatsApp when live**, plus email.
10. **After go-live (not before):** "cómo quieres que trabaje", prospecting offer, tone preferences, sent as a WhatsApp/email invitation "Afina tu asistente".
11. Day 1 / week 1: first-lead and weekly digest alerts.

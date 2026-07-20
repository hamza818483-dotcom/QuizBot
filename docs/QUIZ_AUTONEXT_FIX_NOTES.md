# Quiz Auto-Next Fix — Notes

## Problem
Quiz-e option dagale (answer dile) auto-next quiz aschilo na, ar
Telegram poll-er nijer built-in countdown timer-o dekha jaccilo na.

## Root Cause
Duita quiz system-i (`app.py`-r `qs_get`/`_advance_quiz` DM quiz, ar
`quiz.py`-r `QUIZ_SESSIONS` system) `sendPoll` call korar somoy
`open_period` (Telegram-er nijer auto-close timer) set korto, jeta
bot-er nijer asyncio timeout timer-er shathe protit somoyi expire
hoto (same duration).

Fole ei race condition hocchilo:
- Telegram nijer `open_period` shesh hole poll-take server-side
  auto-close kore dito, thik user answer deyar kaccha-kacchi somoye.
- Shei muhurte user answer dile, Telegram-er poll already closed
  thakar karone shei answer-er `poll_answer` webhook update-i
  ashto na (ba silently drop hoto).
- Fole bot-er `handle_poll_answer` kokhono call-i hoto na, `_advance_quiz`
  trigger hoto na — quiz stall/atke thakto.

Ekta age-r fix ei race dhorte `open_period` shompurno remove kore
diyechilo (`quiz.py`-te) — tate auto-next fix holeo, Telegram-er
nijer visible countdown UI totally gayeb hoye gelo (user-er kache
timer dekha jaccilo na).

## Fix
`open_period` remove na kore, eta emon-vabe rakha holo jate Telegram
kokhono bot-er nijer timeout-er AGE poll close na kore:

- `open_period = bot_er_nijer_timeout + 5 seconds` (buffer)

Fole:
1. Telegram-er visible poll countdown UI abar fire elo.
2. Bot-er nijer timer shobshomoy Telegram-er auto-close-er age
   nijer kaj (advance/skip) shesh kore fele — tai race r hoy na.
3. User option dagale shathe-shathe `poll_answer` thik-vabe ashe,
   `_advance_quiz` call hoy, next quiz instant chole ashe.

### Changed files
- `app.py` — `_send_quiz_question_inner()`: `open_period=QUIZ_Q_SEC` →
  `open_period=QUIZ_Q_SEC + 5`
- `quiz.py` — `send_quiz_question()`: `open_period` add kora holo
  (`session["timer"] + 5`), age eta completely off chilo.

### Commits
- `f1623a3` — original buggy fix: removed `open_period` entirely
  (auto-next fixed, but countdown UI lost)
- `b4766f8` — final fix: restored `open_period` with safe buffer
  (both countdown UI + instant auto-next working together)

## Key takeaway
Bot-er nijer timeout ar Telegram-er `open_period` kokhono exact
same duration-e set kora jabe na — always bot-er timeout-take
`open_period`-er theke choto rakhte hobe (kom-pokkhe 3-5s gap),
nahole answer-drop race abar ferot ashte pare.

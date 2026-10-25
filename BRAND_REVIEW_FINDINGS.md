# Brand / Product Name Review — auto_convert

Review of the repo/product name and marketplace/trademark risk. **No code changes were made.**
Findings are grounded in live marketplace checks (Chrome Web Store, Firefox AMO) and trademark
sources; anything that needs a final legal clearance is flagged as "verify".

Baseline: HEAD `95f74e9`, suite 139/139. This is a naming/branding pass only.

---

## 1. The requested candidates — both are problematic

### 1a. `axe` ❌ (avoid)
- **Taken in the exact same space.** There is already an AI-powered vocal/stem-separation tool
  branded **"Axe"** circulating in this domain, and separately **axe DevTools** (Deque) is a very
  prominent Chrome + Firefox extension — ~300k users, high rating. Both are live marketplace entries
  in the same stores we plan to ship into.
- Even ignoring collision, `axe` is a common dictionary word → weak as a *distinctive* trademark and
  easy to get lost next to axe DevTools. Not recommended.

### 1b. `yax` ❌ (avoid)
- **Taken by an active company.** "Yax Software" is a real, established software house
  (Wrocław, Poland; founded ~2009/2014) with prominent web presence at `yaxint.com` and
  `yax.software`. A three-letter word mark in their hands makes registering/marketing ours risky.
- **No intuitive meaning.** The pitch was a "very light YT reference," but "yax" carries no natural
  pronunciation or etymology tied to audio/YouTube (unlike, say, a real portmanteau). It's memorable
  only by osmosis — hard to explain verbally ("Y-A-X?"). Short marks are also harder to trademark.

**Bottom line for both:** neither is broadly available, and `axe` additionally collides with a direct
competitor concept. Do not build the brand on either.

---

## 2. Hard constraint: do NOT put "YouTube" / "YT" in the wordmark

This isn't just preference — it's an explicit Google rule we should design around, and there is a
registered mark behind it:

- **Google YouTube Branding Guidelines (official):** *"You must never use YouTube name or any
  abbreviation, acronym, or variant of the word YouTube, such as **YT** … in conjunction with the
  overall name of your application."* You may only *reference that it works for* YouTube ("a great app
  for YouTube"), not fold YT into the product's own name.
- The **"YT" logo is a registered trademark** (Canadian TM registration TMA1058994, CIPO).

→ This directly kills any candidate built on "YT" as a brand element: **YTSplitter, YTStems, YTCut**,
etc. would walk into Google's mark and look like an official YouTube product. The safe pattern is to
keep the *brand wordmark* generic/derived and only describe the source in listings ("…for YouTube
videos"). (A "light YT reference" inside `yax` doesn't use the letters, which is why yax isn't blocked
by this rule — but it's still taken by Yax Software.)

---

## 3. The space is crowded — descriptive names already exist

The Chrome Web Store / Firefox AMO are full of "stem splitter for YouTube" products. Confirmed live:

| Product | Where | Notes |
|---|---|---|
| **Riffloop** | Chrome (AI stem splitter, vocal remover) | Direct competitor |
| **Chord Lens** | Web / Chrome (six stems) | Descriptive "lens" suffix pattern |
| **Stem Supply** | Chrome | "Stem + noun" naming |
| **Splitter.ai** | Web/Chrome | ".ai" productized name |
| **ClipSaw** | Web (YouTube cutter/trimmer) | **"clip + saw"** taken — don't reuse bare wordmark |
| **AudioSaw** | Web (83 on-device audio tools) | "audio + saw" live brand, overlapping scope |
| **StemSplitPro** | Mac app (on-device separation) | "stem split" taken-ish |

Implication: a purely descriptive name ("Song Splitter", "Drum Separator") will collide or look like a
clone. The winning pattern in this space is **distinctive brand wordmark + descriptive tagline**, e.g.
`Riffloop`, `Chord Lens`, `Splitter.ai`. Aim for the same shape: a short, ownable name plus a plain-English
descriptor so users still find it via search ("YouTube stem splitter").

---

## 4. Candidate alternatives (shortlist)

Availability of any *invented* name can't be fully cleared by web search alone — **verify each against
the Chrome Web Store search box + Firefox AMO + a trademark office (USPTO ESD/TEAS or WIPO)** before
committing to one. The list below is organized by pattern; pick a distinctive wordmark and append the
descriptive tagline from §5.

### 4a. Distinctive coined / partial-portmanteau names (best availability odds)
These avoid dictionary-word collisions and don't use "YT". Treat as brand words + descriptor:
- **Stemly** — evokes "stem" + friendly suffix; distinct, easy to spell once seen.
- **Splitfy** — "split" + action-suffix like Splitter.ai; memorable verb-y name.
- ~~**Voxcut**~~ — dropped: the app does not cut vocals; it produces *drum-only* and *drumless* tracks. The wordmark would lie about what it does.
- ~~**Vidstem**~~ — dropped (per user): "video + stem" is honest but reads like a clip/trim tool, less distinctive than the spectrum/hole-punch metaphors below.
- **Drillbit / Punchhole / Borehole** — hole-punch metaphor explored separately (§4d); bare forms are taken-ish in other domains (see §4d).
- ~~**Tracklet**~~ — dropped: too generic/abstract, no marketplace presence or meaning to lean on.

### 4b. Generic-source nod (clip/video/song) + action verb — safe, discoverable
Uses non-trademarked source words ("clip"/"video"/"song"), so it sidesteps the YouTube-mark rule while
still reading as a media tool:
- **Clipcut** / **Clipkit** — "clip" (generic) + brand suffix; distinct from clipsaw.com.
- **Songsplit** / **SongSaw** — descriptive but check AMO/CWS for exact wordmark collisions first.

### 4c. Abstract / brandable (lowest collision, hardest to explain)
- A short invented mark (e.g., a made-up 3–5 letter name with a memorable pronunciation). Lowest risk of
  trademark clash, but users won't guess the meaning — you'd lead with the descriptor in listings anyway.

**Recommended shape:** pick **one distinctive wordmark**, then list it as
`<Wordmark> · <descriptor>` (e.g., "Stemly · YouTube stem splitter" or "Clipkit · cut stems from any song").
The descriptor does the SEO/discoverability work while the brand name stays clear of axe/yax/Clipsaw/YT.

Two metaphor directions were explored this pass (**§4d hole-punch / awl**, **§4e prism / spectrum**).
Both metaphors are *conceptually* strong for source separation, but as bare wordmarks they are saturated
or weak — so each should be carried by a **coined mark + descriptor**, not the dictionary word itself.

### 4d. Hole-punch / awl metaphor

The idea: an **awl** makes one clean hole through leather; our app drills precise *holes* in a mix —
a drum-only isolate (the hole) plus a drumless track (what's left around it). Honest and memorable.

Bare wordmark problems (live-checked):
- **`awl`** — a real dictionary word → weaker distinctive mark, awkward to say aloud ("oh-uhl?"),
  no audio meaning. Already used by **awl.studio** (a live standalone-tool brand) and an open-source
  "Agentic Web Labs" extension that adopts the AWL acronym. Not recommended as a bare mark.
- **`drillbit`**, **`punchhole`**, **`borehole`** — prior marks in other domains; `punchkit` is a
  **live Apple music-production app** that "separate recordings into four stems on device" (direct scope overlap).

To keep the metaphor, coin a distinctive mark and append the descriptor. If you like this direction,
I can run full Chrome Web Store + Firefox AMO + USPTO verification on a specific coined candidate before committing.

### 4e. Prism / spectrum metaphor

The idea: white light → dispersed into the color **spectrum**; our app splits one mix into the "color"
spectrum of four stems (drums/bass/other/vocals). Strong visual for a prism/filter concept.

Bare wordmark problems (live-checked) — **`prism` is saturated by direct competitors** doing exactly this:
- **Prism — AI Stem Separation** (MueSync, `muesync.com/tools/prism`) — live local stem-sep tool, same scope.
- **tune-prism** (`hedonhermdev/tune-prism`, GitHub) — splits a track into 4 stems using the HTDemucs model *we* use.
- Lalal.ai uses "PRISM" as one of its split engines. `spectra` is also taken (a live Chrome extension, but an
  accessibility screen reader — different category).

So keep the concept but coin a distinctive mark + descriptor rather than bare "prism". Same offer as §4d:
pick a coined candidate and I'll verify it before committing.

---

## 5. Descriptive taglines / listing names (safe, non-infringing)

These describe *what* we do and *for whom*, without using "YouTube" inside our own wordmark:
- "`<Wordmark>` — split drums & no-drums from any song"
- "`<Wordmark>` · AI stem separation for music videos"
- "`<Wordmark>` · turn a YouTube track into stems, locally"  ← fine to say *for* YouTube (Google allows this)

Note: the **Chrome Web Store listing title** and **Firefox AMO name** are display strings and can safely
mention "YouTube"; only the *brand wordmark/logo lockup* should avoid it.

---

## 6. Trademark / registered-name risk inside the repo

The code legitimately references real, third-party product/tool names — that's functional and fine, **as
long as we never adopt any of them as our brand/marketing name**. Flagged for awareness:

| Name in repo | Status | Risk if used as OUR brand |
|---|---|---|
| `yt-dlp` (binary) | Open-source tool (pucnop/yt-dlp). Used everywhere we download. | Low — it's a dependency, not our mark. Don't put "yt-dlp" in the wordmark or logo. |
| `htdemucs` / `Demucs` | Facebook Research model family ("Demucs") + the htdemucs project. Invoked as `python -m htdemucs`. | Medium — "Demucs" is Meta's registered project name. Keep it as a functional engine identifier only; never in our brand/logo. |
| `UVR5` / `Ultimate Vocal Remover` | Real published open-source app (The1812s). We detect/reuse an existing install. | Medium — it's a competing vocal-separation tool with its own market presence. Referencing the *install path* is fine; don't brand ourselves as "UVR" or claim affiliation. |
| `ffmpeg` / `ffprobe` | FFmpeg project binaries (lowercase convention). Also "FFmpeg" is a registered-ish brand name. | Low — we always use lowercase binary names and never call them a verb/logo. Stay lowercase, descriptive only. |
| `libmp3lame` | Codec (Xiph/OpenMPT). Generic codec identifier. | Negligible. |

**Rule of thumb:** keep all these as *code/dependency identifiers* (lowercase, in comments/docstrings), and
build the product brand from §4–§5 instead. Never render any of them as our logo, app title, or extension
name.

---

## 7. Action items / next steps (not done here)

1. **Pick a wordmark** from §4a/§4b (§4d hole-punch and §4e prism are *metaphors* — pick the idea, then coin
   a distinctive mark to carry it before verifying). Reserve it: search the Chrome Web Store + Firefox AMO by
   name, then check USPTO TESS / EUIPO / WIPO for a registered mark in class 9 (software/extensions).
2. **Confirm `axe` is off** — don't reopen it; there's both an extension and a competitor concept named Axe.
2. **Confirm `axe` is off** — don't reopen it; there's both an extension and a competitor concept named Axe.
3. **Drop "YT" from the wordmark** per Google's explicit guideline (§2); keep it only as a descriptor.
4. Once a name is chosen, I can help wire it into: `README.md`, `AGENTS.md`, `bootstrap.py` banner,
   `provision.py` output messages, the browser extension `manifest.json` (name/short_name), and any
   popup/options copy — without touching logic.

---

## Summary table

| Candidate | Verdict | Reason |
|---|---|---|
| **auto_convert** (current) | Generic, available, but bland & not marketplace-friendly | Fine as repo slug; weak as a product brand |
| **axe** | ❌ Avoid | Taken by axe DevTools + direct competitor "Axe"; dictionary word = weak mark |
| **yax** | ❌ Avoid | Taken by Yax Software (Poland); no intuitive meaning |
| **ClipSaw / AudioSaw** | ❌ Bare wordmark taken | clipsaw.com, audiosaw.com are live products in adjacent scope |
| **YTSplitter / YT* names** | ❌ Trademark-blocked | Google prohibits "YT" in app names; "YT" logo is a registered mark |
| **Stemly / Splitfy / Clipkit** | ✅ Shortlist (verify live) | Distinctive coined marks, no YT letters, avoid axe/yax/Clipsaw collisions |
| ~~Voxcut~~ | ❌ Dropped | App makes drum-only & drumless tracks, not a vocal cut; wordmark would mislead about what it does |
| ~~Vidstem~~ | ❌ Dropped (per user) | "video + stem" honest but reads clip/trim-ish; less distinctive than the spectrum/hole-punch metaphors (§4d–§4e) |
| **awl** (hole-punch metaphor, §4d) | Concept ✅ · bare mark ⚠️ | Nice metaphor; `awl` is a dictionary word already used by awl.studio and awkward to say — carry the idea via a coined mark + descriptor |
| **prism / spectrum** (filter metaphor, §4e) | Concept ✅ · bare mark ❌ | Strong visual; but `prism` saturated by direct stem-sep competitors (MueSync Prism, tune-prism, Lalal PRISM engine), `spectra` taken — carry the idea via a coined mark + descriptor |

Direction: adopt a **distinctive coined wordmark + plain-English descriptor**, keep all real tool names
(yt-dlp, htdemucs/Demucs, UVR5, ffmpeg) as functional identifiers only, and never fold "YouTube"/"YT"
into the brand lockup.

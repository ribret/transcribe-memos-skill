---
name: transcribe-memos
description: Transkribiert neue Aufnahmen aus der macOS-Sprachmemos-App mit noScribe im präzisen Modus, standardmäßig mit Zeitmarken und Markierung überlappender Sprache. Unterstützt .m4a- und .qta-Audiodateien. Beim ersten Lauf installiert der Skill bei Bedarf noScribe halbautomatisch, prüft Rosetta2 und führt einen kurzen Einrichtungsdialog durch (Basispfad, Sprachmemos-Ordnerfilter, Umgang mit Originalen, Sprache, Transkript-Features). Danach findet er alle noch nicht verarbeiteten Aufnahmen aus dem konfigurierten Ordner, transkribiert sie und vergibt inhaltlich passende Dateinamen (YYYY-MM-DD + max. 4 Wörter). Manuell auslösen mit /transcribe-memos oder bei „Sprachmemos transkribieren", „voice memos transcribe", „Memos transkribieren" o.Ä.
---

# transcribe-memos

Portabler macOS-Skill: transkribiert neue Sprachmemos-Aufnahmen mit [noScribe](https://github.com/kaixxx/noScribe) headless, mit Ordnerfilter, Auto-Installation und Fortschrittsanzeige.

## Wann dieser Skill greift

Nur auf explizite manuelle Anfrage — **nie automatisch**:
- „/transcribe-memos"
- „neue Sprachmemos transkribieren", „Memos transkribieren"
- „voice memos transcribe"

## Wann ein anderer Skill richtig wäre (Übergangs-Routing)

Dieser Skill verarbeitet **nur lokale Audio-Dateien** (`.m4a`, `.qta`) aus dem konfigurierten Sprachmemos-Ordner. Bei abweichendem Medium nicht selbst probieren — Claude weist Richard auf den passenden Skill hin und schlägt vor, zu wechseln:

| Medium / URL-Pattern | Richtiger Capture-Pfad | Status |
|----------------------|------------------------|--------|
| YouTube-URLs (`youtube.com/watch`, `youtu.be/`) | Skill `transcribe-youtube` | aktiv |
| **Apple Podcasts-URLs** | yt-dlp ApplePodcasts-Extractor → `.m4a` in den hier konfigurierten Eingangsordner → diesen Skill triggern | **aktiv** (verifiziert 2026-05-24) |
| Andere Podcast-URLs (Spotify, RSS-Feeds, etc.) | yt-dlp probieren; bei Erfolg analog. Bei Misserfolg: RSS-Feed-Workaround | teilweise |
| LinkedIn-Posts, Web-Artikel, TED-Talks | Aktuell: WebFetch ad-hoc. Geplant: `clip-web`-Skill | offen |

**Konkretes yt-dlp-Snippet für Apple Podcasts** (aus dem `transcribe-youtube`-venv, oder beliebigem yt-dlp ≥ 2024):
```bash
yt-dlp --extract-audio --audio-format m4a \
  -o "$EINGANGSORDNER/YYYY-MM-DD Titel.m4a" \
  "https://podcasts.apple.com/.../id...?i=..."
```
Datei landet als `.m4a` im Sprachmemos-Eingangsordner und wird beim nächsten Lauf automatisch mitverarbeitet.

Übergangs-Verhalten gilt, bis der Skill-Orchestrierungs-Layer aktiv ist (siehe Memory `project-skill-orchestration-layer` + Inventar `[[Claude Skills]]` im Memex). Dann routet ein Meta-Agent automatisch, manuelles Hinweisen entfällt.

---

## Schritt 0 — Plattform-Check

```bash
[ "$(uname)" = "Darwin" ] || { echo "Skill funktioniert nur auf macOS."; exit 1; }
ARCH=$(uname -m)
[ "$ARCH" = "arm64" ] || echo "WARNUNG: noScribe ab v0.7 unterstützt nur Apple Silicon. Auf Intel ggf. ältere Version aus dem GitHub-Repo manuell installieren."
```

---

## Schritt 1 — noScribe-Installation prüfen und ggf. einrichten

### 1.1 Suchen

```bash
NOSCRIBE=""
if command -v noScribe >/dev/null 2>&1; then
  NOSCRIBE=$(command -v noScribe)
elif [ -x "/Applications/noScribe.app/Contents/MacOS/noScribe" ]; then
  NOSCRIBE="/Applications/noScribe.app/Contents/MacOS/noScribe"
elif [ -x "$HOME/Applications/noScribe.app/Contents/MacOS/noScribe" ]; then
  NOSCRIBE="$HOME/Applications/noScribe.app/Contents/MacOS/noScribe"
else
  FOUND=$(mdfind "kMDItemFSName == 'noScribe.app' && kMDItemKind == 'Programm'" 2>/dev/null | head -1)
  [ -n "$FOUND" ] && NOSCRIBE="$FOUND/Contents/MacOS/noScribe"
fi
echo "${NOSCRIBE:-NICHT GEFUNDEN}"
```

### 1.2 Installation (wenn nicht gefunden)

noScribe wird **nicht über GitHub-Releases** verteilt, sondern über SWITCHdrive (Schweizer Cloud). Eine vollautomatische Download-URL ist unzuverlässig (Anbieter-Seite ist ownCloud-basiert, dynamische Links). Daher **halbautomatischer Flow**:

1. Hole die aktuelle Download-Information vom GitHub-Repo:

```bash
LATEST_TAG=$(curl -s https://api.github.com/repos/kaixxx/noScribe/releases/latest | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name',''))")
echo "Aktuellste noScribe-Version: $LATEST_TAG"
```

2. Den User fragen, ob auto-installiert werden soll (`AskUserQuestion`):
   - „noScribe ist nicht installiert. Soll der Skill dich durch die Installation führen?"
   - Optionen: **Ja, durch Installation führen** / **Nein, später manuell** (Skill bricht ab)

3. Bei Ja: Browser zur Download-Seite öffnen und User anweisen:

```bash
open "https://github.com/kaixxx/noScribe#installation"
```

   Klare Schritt-für-Schritt-Anweisung an den User:
   > „1) Auf der geöffneten Seite folge dem Link zum SWITCHdrive-Download (für deine Architektur: Apple Silicon).
   > 2) Lade die `.dmg`-Datei nach `~/Downloads/` herunter.
   > 3) Sag mir Bescheid mit ‚fertig' / ‚weiter', sobald die Datei da ist."

4. Auf User-Bestätigung warten (mit `AskUserQuestion`: „DMG heruntergeladen?" → ja/abbrechen).

5. Neueste `.dmg` in Downloads finden und installieren:

```bash
DMG=$(ls -t "$HOME/Downloads"/noScribe*.dmg 2>/dev/null | head -1)
[ -z "$DMG" ] && { echo "Keine noScribe-DMG in ~/Downloads gefunden. Bitte herunterladen und erneut versuchen."; exit 1; }

# Mounten, .app(s) kopieren, abhängen, DMG löschen
MOUNT=$(hdiutil attach "$DMG" -nobrowse -quiet | tail -1 | awk -F'\t' '{print $NF}' | sed 's/^ *//')
echo "Mount: $MOUNT"
for app in "$MOUNT"/*.app; do
  [ -d "$app" ] && cp -R "$app" /Applications/
done
hdiutil detach "$MOUNT" -quiet
rm -f "$DMG"
echo "noScribe installiert nach /Applications/"
NOSCRIBE="/Applications/noScribe.app/Contents/MacOS/noScribe"
```

6. Erste Ausführung im Hintergrund verifizieren:

```bash
"$NOSCRIBE" --help-models >/dev/null 2>&1 && echo "OK" || echo "Installation fehlerhaft."
```

### 1.3 Rosetta2 prüfen (nötig für ffmpeg in noScribe)

```bash
arch -x86_64 /usr/bin/true 2>/dev/null && echo "Rosetta2 vorhanden" || echo "FEHLT"
```

Wenn fehlt: dem User die Installationszeile geben (braucht sudo, daher User-Aktion):

> „Bitte einmalig im Terminal ausführen: `softwareupdate --install-rosetta --agree-to-license`. Dann diesen Skill erneut aufrufen."

Skill abbrechen, bis Rosetta da ist.

### 1.4 Full Disk Access prüfen

Host-App via Parent-Prozess-Kette detektieren (Cursor / Terminal / iTerm / Claude.app / Warp / VS Code), dann Read-Test:

```bash
ls "$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/" >/dev/null 2>&1
```

Bei Fehler: konkreten FDA-Hinweis für die erkannte Host-App geben und abbrechen.

---

## Schritt 2 — Konfiguration laden oder Setup

Konfig-Pfad: `~/.config/transcribe-memos/config.json`

Wenn vorhanden: laden, weiter mit Schritt 3.

Wenn nicht: **Einrichtungsdialog** mit folgenden Fragen (in einem `AskUserQuestion`-Aufruf gebündelt, max. 4 Fragen):

### Frage 1 — Basispfad

„Wo sollen die Sprachmemos-Transkripte abgelegt werden? (Es werden Unterordner `transcribe/` und `transcribed/` angelegt.)"
- A: `~/Documents/Sprachmemos/` *(Standard)*
- Other: eigener Pfad

### Frage 2 — Sprachmemos-Quellordner

**Erst die in Sprachmemos angelegten Ordner aus der CoreData-DB auslesen**, dem User als Optionen anbieten:

```bash
DB="$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db"
if [ -f "$DB" ]; then
  # Folder-Namen + Anzahl Aufnahmen pro Folder
  /usr/bin/sqlite3 "$DB" "SELECT f.ZENCRYPTEDNAME, COUNT(r.Z_PK) FROM ZFOLDER f LEFT JOIN ZCLOUDRECORDING r ON r.ZFOLDER = f.Z_PK GROUP BY f.Z_PK ORDER BY f.ZENCRYPTEDNAME;" 2>/dev/null
  # Fallback-Schema, falls obiges nichts liefert: Spaltennamen variieren je macOS-Version
  /usr/bin/sqlite3 "$DB" ".schema ZFOLDER" 2>/dev/null
fi
```

Wichtig: Spaltennamen in CoreData wechseln zwischen macOS-Versionen (`ZNAME`, `ZENCRYPTEDNAME`, `ZCUSTOMLABEL` etc.). Wenn die direkte Abfrage leer ist, hole das Schema mit `.schema ZFOLDER` und passe den Spaltennamen dynamisch an.

Dann dem User anbieten:
- A: **Alle Aufnahmen** (kein Filter)
- B-D: bis zu 3 konkrete Ordner-Namen aus der DB
- Other: Ordner-Name als Text eingeben

Wenn DB nicht lesbar (FDA-Problem trotz Check, oder DB fehlt): nur „Alle Aufnahmen" anbieten plus Texteingabe-Möglichkeit.

### Frage 3 — Umgang mit Originalen

- A: **Behalten** in Sprachmemos *(Standard)*
- B: **Löschen aus Sprachmemos, Audio neben Transkript archivieren**
- C: **Komplett löschen** (nur Transkript bleibt)

### Frage 4 — Sprache

- A: **Deutsch** *(Standard)* — `de`
- B: **Englisch** — `en`
- C: **Auto** — `auto`

### Frage 5 — Optionale Transkript-Features (zweiter `AskUserQuestion`-Aufruf, weil Limit 4 Fragen/Call)

**multiSelect: true**, beide Optionen sind die empfohlene Default-Wahl:

„Welche optionalen Features soll das Transkript enthalten? (Empfehlung: beide aktivieren.)"
- **Zeitmarken im Transkript** *(empfohlen)* — vor jedem Segment wird die Zeitstempel-Position eingefügt
- **Überlappende Sprache markieren** *(empfohlen)* — wenn mehrere Personen gleichzeitig sprechen, wird das im Transkript gekennzeichnet

Wenn der User abbricht oder nichts auswählt: **beide standardmäßig aktiv** setzen (Standard-Empfehlung gilt).

### Konfig schreiben

```bash
CONFIG="$HOME/.config/transcribe-memos/config.json"
mkdir -p "$(dirname "$CONFIG")"
mkdir -p "<basispfad>/transcribe" "<basispfad>/transcribed"
cat > "$CONFIG" <<EOF
{
  "version": 3,
  "noscribe_path": "<NOSCRIBE>",
  "transcribe_dir": "<basispfad>/transcribe",
  "transcribed_dir": "<basispfad>/transcribed",
  "source_folder": "<DB-Ordnername oder leer für alle>",
  "language": "<de|en|auto>",
  "model": "precise",
  "speaker_detection": "auto",
  "timestamps": true,
  "overlapping": true,
  "originals": "<keep|delete_with_archive|delete>",
  "audio_extensions": ["m4a", "qta"]
}
EOF
```

Bestätigen: Konfig liegt unter `~/.config/transcribe-memos/config.json`, editierbar.

---

## Schritt 3 — Neue Aufnahmen finden (mit Ordnerfilter)

Aus Konfig laden: alle Felder.

Quelle finden (mit konfigurierten Endungen, Default `m4a` + `qta`):
```bash
# Endungen aus Konfig zu find-Pattern bauen
EXTS=$(jq -r '.audio_extensions[]' "$CONFIG" 2>/dev/null)
FIND_EXPR=()
for ext in $EXTS; do
  FIND_EXPR+=(-o -iname "*.${ext}")
done
# führendes -o entfernen
[ "${FIND_EXPR[0]}" = "-o" ] && FIND_EXPR=("${FIND_EXPR[@]:1}")

SOURCE=""
for p in "$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings" \
         "$HOME/Library/Containers/com.apple.VoiceMemos/Data/Library/Application Support/com.apple.voicememos/Recordings"; do
  if [ -d "$p" ] && [ -n "$(find "$p" -maxdepth 1 -type f \( "${FIND_EXPR[@]}" \) -print -quit 2>/dev/null)" ]; then
    SOURCE="$p"; break
  fi
done
```

**Wenn `source_folder` gesetzt** ist: nur Aufnahmen aus diesem Ordner via DB-Query:

```bash
DB="$SOURCE/CloudRecordings.db"
# Hol die ZPATH-Werte für Aufnahmen im gewünschten Folder
# Spaltennamen ggf. an Schema anpassen (ZNAME vs ZENCRYPTEDNAME je nach macOS-Version)
FILES=$(/usr/bin/sqlite3 "$DB" "SELECT r.ZPATH FROM ZCLOUDRECORDING r JOIN ZFOLDER f ON r.ZFOLDER = f.Z_PK WHERE f.ZENCRYPTEDNAME = '<source_folder>';" 2>/dev/null)
```

Die ZPATH-Werte sind relativ zu `$SOURCE` — entsprechend zusammensetzen. Sprachmemos-DB enthält in der Praxis nur `.m4a`-Aufnahmen; `.qta`-Dateien (oder andere konfigurierte Endungen) werden zusätzlich direkt aus dem Source-Verzeichnis berücksichtigt, falls sie dort liegen. Falls die DB-Abfrage leer bleibt: User informieren (Ordner umbenannt?) und auf „alle Aufnahmen mit konfigurierten Endungen" zurückfallen.

**Wenn kein `source_folder`**: alle Dateien mit den konfigurierten Endungen in `$SOURCE`:
```bash
find "$SOURCE" -maxdepth 1 -type f \( "${FIND_EXPR[@]}" \)
```

Idempotenz-Filter über `$TRANSCRIBED/.processed.txt`:
```bash
PROCESSED="$TRANSCRIBED/.processed.txt"; touch "$PROCESSED"
NEW=()
for f in "${CANDIDATES[@]}"; do
  grep -Fxq "$(basename "$f")" "$PROCESSED" || NEW+=("$f")
done
[ ${#NEW[@]} -eq 0 ] && { echo "Keine neuen Aufnahmen."; exit 0; }
```

User informieren: Anzahl neuer Memos und kurzer Hinweis auf Laufzeit.

---

## Schritt 4 — Transkription über den Queue-Runner mit Live-Statusseite

Die Transkription läuft **nicht** mehr als einzelne noScribe-Aufrufe mit `Monitor`-Polling. Zwei empirisch belegte Gründe (2026-07-02, Quellcode-Analyse noScribe 0.7.1):

1. **noScribe wirft den Prozentwert im `--no-gui`-Modus weg.** `set_progress()` steigt bei Headless sofort aus; die Prozente existieren nur in GUI-Widgets, nie in stdout oder Log. Log-Tailing nach Prozent konnte nie funktionieren.
2. **Claude kann während eines Hintergrundjobs nicht live in den Chat tickern.** Der Zug endet, Re-Invoke erst bei Fertigstellung. Deshalb kamen Zwischenstände nie an.

Lösung: das mitgelieferte Skript **`transcribe_queue.py`** (im Skill-Ordner) übernimmt die **gesamte Queue** und rendert eine selbst-aktualisierende, **JS-freie** Statusseite (`_status/index.html`, `<meta refresh>` alle 2 s), die unabhängig vom Claude-Zyklus läuft. Echter Fortschritt kommt aus dem live-geflushten noScribe-Log (`~/Library/Application Support/noScribe/log/<stem>.log`): Sprechererkennung aus den sprachunabhängigen `segmentation:/embeddings:`-Markern, Transkription aus der letzten `[HH:MM:SS]`-Audio-Zeitmarke ÷ Audiolänge.

### 4.1 Jobliste schreiben

`NEW[]` (absolute Quellpfade aus Schritt 3) als JSON-Array in eine Datei schreiben, z. B. im Scratchpad:

```bash
JOBS="<scratch>/jobs.json"
STATUS_DIR="$(dirname "$TRANSCRIBE")/_status"   # neben transcribe/ und transcribed/
MANIFEST="<scratch>/manifest.json"
python3 -c "import json,sys; json.dump(sys.argv[1:], open('$JOBS','w'))" "${NEW[@]}"
```

### 4.2 Runner im Hintergrund starten

Mit `Bash` und **`run_in_background: true`** (eine einzige langlaufende Task für die ganze Queue):

```bash
python3 "$HOME/.claude/skills/transcribe-memos/transcribe_queue.py" \
  --config "$HOME/.config/transcribe-memos/config.json" \
  --jobs "$JOBS" --status-dir "$STATUS_DIR" --manifest "$MANIFEST"
```

Der Runner öffnet die Statusseite automatisch im Browser (`open`). Er kopiert jede Quelle nach `transcribe_dir`, ruft noScribe headless mit den Konfig-Flags auf (`--model`, `--language`, `--speaker-detection`, `--timestamps/--no-timestamps`, `--overlapping/--no-overlapping`, `--no-disfluencies`), liest live das noScribe-Log und schreibt Phase + echten Prozentwert in die Statusseite.

Dem User **einmal** den Pfad zur Statusseite nennen (`$STATUS_DIR/index.html`), dann auf die Fertigstellung der Hintergrund-Task warten. Kein Zwischenstand-Gepolle im Chat nötig, die Statusseite trägt die Live-Info. (`--no-open` unterdrückt das Browser-Öffnen, z. B. bei reinem Batch.)

### 4.3 Manifest lesen und prüfen

Nach Fertigstellung `manifest.json` lesen. Je Job: `{src, name, out_txt, work_audio, state, duration, elapsed, error}`.
- `state == "done"`: in Schritt 5/6 weiterverarbeiten (`out_txt` liegt in `transcribe_dir`).
- `state == "failed"`: **nichts** committen, im Abschlussbericht (Schritt 7) einzeln mit `error` melden.

---

## Schritt 5 — Dateiname generieren

Pro Job mit `state == "done"`: lies die ersten ~1500 Zeichen von `out_txt` (aus dem Manifest). Generiere einen **inhaltlich passenden Dateinamen** mit max. **4 Wörtern**:
- Sprache des Memos beibehalten
- Substantive, keine Füllwörter
- Spaces ok, keine Sonderzeichen außer `-`
- Großschreibung wie zielsprachenüblich

Aufnahmedatum: `REC_DATE=$(stat -f "%Sm" -t "%Y-%m-%d" "<src>")` (Manifest-Feld `src`).

Finaler Name: `<REC_DATE> <kurztitel>.txt`. Bei Kollision: ` (2)`, ` (3)` anhängen.

---

## Schritt 6 — Committen

Pro Job mit `state == "done"` (Felder aus dem Manifest: `out_txt`, `work_audio`, `src`, `name`):

```bash
FINAL_TXT="$TRANSCRIBED/$REC_DATE <kurztitel>.txt"
mv "<out_txt>" "$FINAL_TXT"

EXT="${name##*.}"
case "$ORIGINALS" in
  keep)                rm -f "<work_audio>" ;;
  delete_with_archive) mv "<work_audio>" "$TRANSCRIBED/$REC_DATE <kurztitel>.$EXT"; rm -f "<src>" ;;
  delete)              rm -f "<work_audio>"; rm -f "<src>" ;;
esac

echo "<name>" >> "$PROCESSED"
```

Die noScribe-Logdatei unter `~/Library/Application Support/noScribe/log/<stem>.log` bleibt liegen (nützlich für Nachschau); sie ist nicht Teil von `transcribe_dir` und muss nicht aufgeräumt werden.

Bei `delete`/`delete_with_archive` zusätzlich: den DB-Eintrag in `CloudRecordings.db` für diese Aufnahme behält Sprachmemos als „verwaiste" Referenz. Das ist unkritisch — die App räumt das beim nächsten Start auf. Falls Sprachmemos die Aufnahme nach `rm` doch wieder herstellt (über iCloud-Re-Sync), greift der `.processed.txt`-Filter beim nächsten Lauf.

---

## Schritt 7 — Abschlussbericht

Zusammenfassung an den User:
- Anzahl verarbeitete Memos
- Liste der erzeugten `.txt`-Dateien als `[Name](pfad)`-Links
- Eventuelle Fehler einzeln

---

## Konfiguration ändern

`~/.config/transcribe-memos/config.json` editierbar. Zum kompletten Reset (z. B. neuer Sprachmemos-Ordner): JSON löschen → nächster Skill-Aufruf startet Setup erneut.

## Distribution

Ordner [`~/.claude/skills/transcribe-memos/`](.claude/skills/transcribe-memos/) in das eigene `~/.claude/skills/`-Verzeichnis kopieren. Beim ersten Aufruf macht der Skill alles Nötige (noScribe-Install, Rosetta-Check, FDA-Hinweis, Setup-Dialog).

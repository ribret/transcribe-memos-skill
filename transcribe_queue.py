#!/usr/bin/env python3
"""
transcribe_queue.py — deterministischer Queue-Runner mit Live-Statusseite
fuer den transcribe-memos Skill.

Warum das existiert:
- noScribe gibt den Prozentwert nur an GUI-Widgets, im --no-gui-Modus wird er
  verworfen. Fortschritt ist aber aus der live-geflushten noScribe-Logdatei
  ablesbar: sprachunabhaengige `segmentation:/embeddings:`-Marker (Sprecher-
  erkennung) und eckige `[HH:MM:SS]`-Audio-Zeitmarken (Transkription).
- Claude Code kann waehrend eines Hintergrundjobs nicht live in den Chat
  tickern (der Zug endet, Re-Invoke erst bei Fertigstellung). Deshalb rendert
  dieser Runner eine selbst-aktualisierende, JS-freie status/index.html
  (meta-refresh), die unabhaengig vom Claude-Zyklus laeuft.

Aufruf:
  python3 transcribe_queue.py --config <config.json> --jobs <jobs.json> \
      --status-dir <dir> --manifest <manifest.json> [--no-open]

  jobs.json : JSON-Array absoluter Audio-Quellpfade.

Selbsttest der Parser gegen ein vorhandenes Log (transkribiert nichts):
  python3 transcribe_queue.py --selftest "<pfad/zu/name.log>" [--duration SEC]
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- Log-Parsing (sprachunabhaengig, deshalb robust ueber UI-Sprachen) -----

# Sprechererkennung: pyannote gibt roh "segmentation: N%" / "embeddings: N%"
RE_DIAR = re.compile(r'(segmentation|embeddings|speaker_counting|discrete_diarization):\s*(\d+)%')
# Transkription: Segmente werden als "Sxx: [HH:MM:SS] Text" geloggt.
# Die eckige Klammer kommt NUR in der Transkriptionsphase vor (Diarisierungs-
# Zeilen nutzen "HH:MM:SS.mmm - HH:MM:SS.mmm SPEAKER_x" ohne Klammern).
RE_TS = re.compile(r'\[(\d{2}):(\d{2}):(\d{2})\]')


def _hhmmss_to_sec(h, m, s):
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_log(text, duration_sec, speaker_on):
    """Leitet Phase, Gesamt-Prozent und ein Live-Snippet aus dem Logtext ab.

    Rueckgabe: dict(phase, phase_label, pct, snippet)
    phase in {"prep","speaker","transcribe","unknown"}
    pct: 0..99 (Gesamtfortschritt, gewichtet wie noScribe intern)
    """
    ts_matches = RE_TS.findall(text)
    diar_matches = RE_DIAR.findall(text)

    # Innerer Fortschritt der Sprechererkennung (segmentation = 1. Haelfte,
    # embeddings = 2. Haelfte).
    seg_last = None
    emb_last = None
    diar_done = 'discrete_diarization' in dict((k, v) for k, v in diar_matches) if diar_matches else False
    for k, v in diar_matches:
        if k == 'segmentation':
            seg_last = int(v)
        elif k == 'embeddings':
            emb_last = int(v)
        elif k == 'discrete_diarization':
            diar_done = True
    if emb_last is not None:
        speaker_inner = 50 + emb_last / 2.0
    elif seg_last is not None:
        speaker_inner = seg_last / 2.0
    else:
        speaker_inner = 0.0
    if diar_done:
        speaker_inner = 100.0

    # Transkriptionsfortschritt aus letzter Audio-Zeitmarke.
    trans_inner = 0.0
    if ts_matches:
        last = ts_matches[-1]
        sec = _hhmmss_to_sec(*last)
        if duration_sec and duration_sec > 0:
            trans_inner = min(99.0, sec / duration_sec * 100.0)
        else:
            trans_inner = 0.0

    # Sprachunabhaengige Modell-Marker (Produktnamen werden nicht uebersetzt).
    has_whisper = 'Whisper' in text or 'whisper laden' in text.lower()
    has_pyannote = 'yannote' in text  # matcht "Pyannote" und "PyAnnote"

    # Phase bestimmen. Sobald eckige Zeitmarken auftauchen, laeuft die
    # Transkription. Zwischen fertiger Diarisierung und erster Zeitmarke laedt
    # noScribe das Whisper-Modell -> eigene "Vorbereiten"-Zwischenphase, damit
    # der Balken nicht irrefuehrend auf "Sprecher 100%" haengt.
    if ts_matches:
        phase = "transcribe"
    elif diar_matches and has_whisper:
        phase = "transcribe_prep"
    elif diar_matches:
        phase = "speaker"
    else:
        phase = "prep"

    # Gesamt-Prozent, Gewichtung analog noScribe set_progress():
    #   prep 0-5, speaker 5-50 (nur wenn an), transcribe Rest bis 99.
    if phase == "prep":
        pct = 3.0
        phase_label = "Sprecher-Modell laden" if has_pyannote else "Vorbereiten / Audio umwandeln"
    elif phase == "speaker":
        pct = 5.0 + speaker_inner * 0.45
        phase_label = f"Sprecher erkennen ({int(speaker_inner)}%)"
    elif phase == "transcribe_prep":
        pct = 50.0 if speaker_on else 5.0
        phase_label = "Transkription vorbereiten (Modell laden)"
    else:  # transcribe
        base = 50.0 if speaker_on else 5.0
        span = 49.0 if speaker_on else 94.0
        pct = base + trans_inner / 100.0 * span
        phase_label = f"Transkribieren ({int(trans_inner)}%)"

    pct = max(0.0, min(99.0, pct))

    # Live-Snippet: letztes Stueck transkribierter Text als Lebenszeichen.
    snippet = ""
    if ts_matches:
        # letzte Zeile mit eckiger Zeitmarke nehmen
        for line in reversed(text.splitlines()):
            if RE_TS.search(line):
                # fuehrenden "Sxx: [..]" Teil grob entfernen
                cleaned = re.sub(r'^\s*S\d+:\s*', '', line)
                cleaned = RE_TS.sub('', cleaned).strip()
                snippet = cleaned[-160:]
                break

    return {"phase": phase, "phase_label": phase_label, "pct": pct, "snippet": snippet}


# --- Hilfsfunktionen -------------------------------------------------------

def audio_duration_sec(path):
    """Geschaetzte Audiolaenge in Sekunden via macOS afinfo. 0 wenn unbekannt."""
    try:
        out = subprocess.run(["/usr/bin/afinfo", path], capture_output=True,
                             text=True, timeout=30).stdout
        m = re.search(r'estimated duration:\s*([0-9.]+)\s*sec', out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return 0.0


def fmt_mmss(sec):
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


NOSCRIBE_LOGDIR = os.path.expanduser("~/Library/Application Support/noScribe/log")


# --- HTML-Rendering (JS-frei, meta-refresh) --------------------------------

STATE_BADGE = {
    "queued": ("Wartet", "#6b7280"),
    "running": ("Läuft", "#2563eb"),
    "done": ("Fertig", "#059669"),
    "failed": ("Fehler", "#dc2626"),
}


def render_html(jobs, active_idx, done_all):
    total = len(jobs)
    done_n = sum(1 for j in jobs if j["state"] == "done")
    fail_n = sum(1 for j in jobs if j["state"] == "failed")
    refresh = "" if done_all else '<meta http-equiv="refresh" content="2">'
    overall_pct = int(sum(j.get("pct", 0) for j in jobs) / total) if total else 0
    if done_all:
        overall_pct = 100

    rows = []
    for i, j in enumerate(jobs):
        label, color = STATE_BADGE[j["state"]]
        pct = int(j.get("pct", 0))
        if j["state"] == "done":
            pct = 100
        phase = html.escape(j.get("phase_label", "") if j["state"] == "running" else
                            ("abgeschlossen" if j["state"] == "done" else
                             (j.get("error", "") if j["state"] == "failed" else "in Warteschlange")))
        snippet = html.escape(j.get("snippet", "")) if j["state"] == "running" else ""
        timing = ""
        if j["state"] == "running" and j.get("started"):
            elapsed = time.time() - j["started"]
            eta = ""
            if pct > 1:
                remain = elapsed / pct * (100 - pct)
                eta = f" · ~{fmt_mmss(remain)} übrig"
            timing = f'{fmt_mmss(elapsed)} gelaufen{eta}'
        elif j["state"] == "done" and j.get("elapsed"):
            timing = f'{fmt_mmss(j["elapsed"])} gesamt'
        bar_col = color
        rows.append(f"""
      <div class="row {j['state']}">
        <div class="rowhead">
          <span class="name">{i+1}. {html.escape(j['name'])}</span>
          <span class="badge" style="background:{color}">{label}</span>
        </div>
        <div class="track"><div class="fill" style="width:{pct}%;background:{bar_col}"></div></div>
        <div class="meta"><span>{phase}</span><span>{html.escape(timing)} · {fmt_mmss(j.get('duration',0))} Audio · {pct}%</span></div>
        {f'<div class="snip">… {snippet}</div>' if snippet else ''}
      </div>""")

    status_line = (f"{done_n}/{total} fertig"
                   + (f", {fail_n} Fehler" if fail_n else "")
                   + (" · alles erledigt ✓" if done_all else ""))

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transkription — Status</title>
<style>
  :root {{ color-scheme: dark light; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         margin:0; padding:24px; background:#0f1115; color:#e6e8eb; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:19px; margin:0 0 2px; }}
  .sub {{ color:#9aa0a6; font-size:13px; margin-bottom:18px; }}
  .overall {{ margin:0 0 22px; }}
  .otrack {{ height:10px; border-radius:6px; background:#20242c; overflow:hidden; }}
  .ofill {{ height:100%; background:#2563eb; transition:width .3s; }}
  .row {{ background:#161a21; border:1px solid #232833; border-radius:10px;
          padding:12px 14px; margin-bottom:10px; }}
  .row.running {{ border-color:#2563eb55; }}
  .row.done {{ opacity:.72; }}
  .rowhead {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
  .name {{ font-weight:600; }}
  .badge {{ color:#fff; font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px; }}
  .track {{ height:7px; border-radius:5px; background:#20242c; overflow:hidden; margin:9px 0 6px; }}
  .fill {{ height:100%; transition:width .3s; }}
  .meta {{ display:flex; justify-content:space-between; color:#9aa0a6; font-size:12px; gap:10px; }}
  .snip {{ color:#c7ccd3; font-size:12.5px; margin-top:7px; font-style:italic;
           white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .foot {{ color:#6b7280; font-size:11px; margin-top:16px; text-align:center; }}
</style></head><body><div class="wrap">
  <h1>Sprachmemos transkribieren</h1>
  <div class="sub">{status_line}</div>
  <div class="overall"><div class="otrack"><div class="ofill" style="width:{overall_pct}%"></div></div></div>
  {''.join(rows)}
  <div class="foot">Aktualisiert sich automatisch alle 2&nbsp;s{'' if not done_all else ' · fertig, kein Auto-Refresh mehr'}</div>
</div></body></html>"""


def write_status(status_dir, jobs, active_idx, done_all):
    idx = Path(status_dir) / "index.html"
    tmp = Path(status_dir) / ".index.tmp"
    tmp.write_text(render_html(jobs, active_idx, done_all), encoding="utf-8")
    os.replace(tmp, idx)  # atomar
    return idx


# --- Hauptlauf -------------------------------------------------------------

def run(config_path, jobs_path, status_dir, manifest_path, do_open):
    cfg = json.loads(Path(config_path).read_text())
    noscribe = cfg["noscribe_path"]
    transcribe_dir = cfg["transcribe_dir"]
    lang = cfg.get("language", "de")
    model = cfg.get("model", "precise")
    speaker = cfg.get("speaker_detection", "auto")
    speaker_on = speaker != "none"
    timestamps = cfg.get("timestamps", True)
    overlapping = cfg.get("overlapping", True)

    os.makedirs(transcribe_dir, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)

    sources = json.loads(Path(jobs_path).read_text())
    jobs = []
    for src in sources:
        p = Path(src)
        jobs.append({
            "src": str(p), "name": p.name, "stem": p.stem,
            "state": "queued", "pct": 0.0, "duration": audio_duration_sec(str(p)),
            "phase_label": "", "snippet": "", "started": None, "elapsed": None,
            "out_txt": str(Path(transcribe_dir) / f"{p.stem}.txt"), "error": "",
        })

    idx = write_status(status_dir, jobs, -1, False)
    if do_open:
        try:
            subprocess.run(["/usr/bin/open", str(idx)], timeout=10)
        except Exception:
            pass

    ts_flag = "--timestamps" if timestamps else "--no-timestamps"
    ov_flag = "--overlapping" if overlapping else "--no-overlapping"

    for i, job in enumerate(jobs):
        work_audio = str(Path(transcribe_dir) / job["name"])
        out_txt = job["out_txt"]
        log_path = os.path.join(NOSCRIBE_LOGDIR, f"{job['stem']}.log")

        try:
            shutil.copy2(job["src"], work_audio)
        except Exception as e:
            job["state"] = "failed"; job["error"] = f"Kopieren fehlgeschlagen: {e}"
            write_status(status_dir, jobs, i, False); continue

        # altes Log entfernen, damit wir frischen Fortschritt parsen
        try:
            if os.path.exists(log_path):
                os.remove(log_path)
        except Exception:
            pass

        cmd = [noscribe, "--no-gui", "--model", model, "--language", lang,
               "--speaker-detection", speaker, ts_flag, ov_flag,
               "--no-disfluencies", work_audio, out_txt]

        job["state"] = "running"; job["started"] = time.time()
        write_status(status_dir, jobs, i, False)

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            try:
                if os.path.exists(log_path):
                    txt = Path(log_path).read_text(encoding="utf-8", errors="replace")
                    st = parse_log(txt, job["duration"], speaker_on)
                    job["pct"] = st["pct"]
                    job["phase_label"] = st["phase_label"]
                    job["snippet"] = st["snippet"]
            except Exception:
                pass
            write_status(status_dir, jobs, i, False)
            time.sleep(1.5)

        job["elapsed"] = time.time() - job["started"]
        ok = os.path.exists(out_txt) and os.path.getsize(out_txt) > 0 and proc.returncode == 0
        if ok:
            job["state"] = "done"; job["pct"] = 100.0
        else:
            job["state"] = "failed"
            job["error"] = job.get("error") or f"noScribe-Exit {proc.returncode}, kein Output"
        write_status(status_dir, jobs, i, False)

    done_all = True
    write_status(status_dir, jobs, -1, done_all)

    manifest = [{
        "src": j["src"], "name": j["name"], "out_txt": j["out_txt"],
        "work_audio": str(Path(transcribe_dir) / j["name"]),
        "state": j["state"], "duration": j["duration"],
        "elapsed": j["elapsed"], "error": j["error"],
    } for j in jobs]
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"FERTIG: {sum(1 for j in jobs if j['state']=='done')}/{len(jobs)} ok. "
          f"Manifest: {manifest_path}")


def selftest(logfile, duration):
    txt = Path(logfile).read_text(encoding="utf-8", errors="replace")
    for dur in ([duration] if duration else [0, 300, 3600]):
        st = parse_log(txt, dur, speaker_on=True)
        print(f"[dur={dur}s] phase={st['phase']:10s} pct={st['pct']:5.1f} "
              f"label={st['phase_label']!r}")
    print("snippet:", st["snippet"][:100])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--jobs")
    ap.add_argument("--status-dir")
    ap.add_argument("--manifest")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--selftest")
    ap.add_argument("--duration", type=float, default=0)
    a = ap.parse_args()

    if a.selftest:
        selftest(a.selftest, a.duration); return
    run(a.config, a.jobs, a.status_dir, a.manifest, not a.no_open)


if __name__ == "__main__":
    main()

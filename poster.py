#!/usr/bin/env python3
"""Sunday Easel: plaatst de volgende post uit wachtrij/ op Instagram en Facebook (feed + story).

Draait in GitHub Actions (.github/workflows/post.yml), dus ook als de laptop dicht is. Alleen standaardbibliotheek.
Beelden gaan via de raw-URL van deze repo op de huidige commit naar Instagram (die haalt alleen openbare URL's op).
  Instagram: carrousel (1-feed, 2-feed) met caption.txt, story.jpg als story
  Facebook:  post met dezelfde twee foto's (zonder hashtags, met klikbare shoplink), story.jpg als paginastory
Reels (post.json "type": "reel") staan als video in GitHub Release 'reels' (niet in de repo: te groot), cover.jpg in de map:
  Instagram: Reel in het raster (share_to_feed, cover.jpg) + dezelfde video als story
  Facebook:  Reel + videostory. Upload binair naar rupload.facebook.com (Meta-docs, gecheckt 21-10-2026).
Een reel volgt in dezelfde run op de post ervoor (een minuut later), dus per dag: 10:00 post + reel, 13:00, 20:00.
Volgorde in de wachtrij = volgorde in het raster, daarom blijft het dambord kloppen, ook als een vak mist.
Elke geslaagde stap gaat direct in post.json; klaar = map naar gepost/ en een regel in log.txt.
De workflow commit dat terug, zodat de volgende run weet wat er al staat.

  python3 poster.py --droog      toon de volgende post, plaats niets
  python3 poster.py --rooster    plaats alleen als er een tijdvak open is dat vandaag nog niet gepost is
  python3 poster.py --nu         plaats de volgende post direct (handmatige run)
Omgeving: PAGE_TOKEN, PAGE_ID, IG_USER_ID (repo-secrets), GITHUB_REPOSITORY, GITHUB_SHA.
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

GRAPH = "https://graph.facebook.com/v26.0"
ROOT = Path(__file__).resolve().parent
WACHTRIJ, GEPOST, LOG = ROOT / "wachtrij", ROOT / "gepost", ROOT / "log.txt"
TZ = ZoneInfo("Europe/Amsterdam")
# Lokale tijd NL. Onderbouwing (18-10-2026): meta-analyse 9 studies (MeetEdgar 2026) werkdagen 11-14 en 17-21 uur,
# Buffer 2026 9 uur sterkste ochtenduur, Sprout 2026 vroege ochtend vermijden. 20:00 NL = 14:00 Amerikaanse oostkust.
ROOSTER = ["10:00", "13:00", "20:00"]
MAX_VERTRAGING = 120   # minuten; GitHub start geplande runs soms later, een gemist vak wordt niet ingehaald
BEELDEN = ("1-feed.jpg", "2-feed.jpg", "story.jpg")
RUPLOAD = "https://rupload.facebook.com"
REEL_PAUZE = 60   # seconden tussen post en reel in dezelfde run


def nu():
    return datetime.now(TZ)


def mappen():
    return sorted((m for m in WACHTRIJ.glob("[0-9][0-9][0-9] *") if m.is_dir()), key=lambda m: m.name)


def huidig_vak():
    t = nu()
    begonnen = [v for v in ROOSTER if t >= t.replace(hour=int(v[:2]), minute=int(v[3:]), second=0, microsecond=0)]
    if not begonnen:
        return None
    v = begonnen[-1]
    start = t.replace(hour=int(v[:2]), minute=int(v[3:]), second=0, microsecond=0)
    return v if (t - start).total_seconds() <= MAX_VERTRAGING * 60 else None


def al_gepost(vak):
    vandaag = nu().date().isoformat()
    return LOG.exists() and any(r.startswith(vandaag) and f" OK [{vak}] " in r for r in LOG.read_text().splitlines())


def get(pad, **params):
    url = f"{GRAPH}/{pad}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def post(pad, token, **velden):
    data = urllib.parse.urlencode({**velden, "access_token": token}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{GRAPH}/{pad}", data=data), timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        fout = json.loads(e.read() or b"{}").get("error", {})
        raise RuntimeError(f"Graph {e.code} op /{pad}: {fout.get('message')} "
                           f"({fout.get('error_user_msg') or fout.get('type')}, code {fout.get('code')})")


def wacht_klaar(container, token, maxs=180):
    for _ in range(maxs // 3):
        s = get(container, fields="status_code,status", access_token=token)
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram-container {container}: {s.get('status')}")
        time.sleep(3)
    raise RuntimeError(f"Instagram-container {container} niet klaar na {maxs} s")


def fb_tekst(caption):
    regels = [r for r in caption.strip().splitlines() if not r.strip().startswith("#")]
    regels = [("Shop the print: https://sundayeasel.etsy.com" if r.startswith("Link in bio") else r) for r in regels]
    return "\n".join(regels).strip()


def raw_url(pad):
    repo, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{urllib.parse.quote(str(pad.relative_to(ROOT)))}"


def soort(m):
    return json.loads((m / "post.json").read_text()).get("type", "post")


def video_url(p):
    return f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'silprins-a11y/sundayeasel-media')}/releases/download/{p['release']}/{p['video']}"


def rupload(url, tok, data):
    """Binaire upload naar Meta (Instagram resumable upload of Facebook video-upload)."""
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(len(data)),
        "Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"upload {e.code} naar {url.split('?')[0]}: {e.read()[:300]!r}")


def wacht_raw(urls):
    for u in urls:   # raw-cache: even wachten tot de commit-URL antwoordt
        for _ in range(20):
            try:
                with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=15):
                    break
            except urllib.error.HTTPError:
                time.sleep(3)


def stappen(m, volgorde, doe):
    """Voert de stappen uit die nog niet in post.json staan; elke geslaagde stap wordt direct bewaard."""
    pj = m / "post.json"
    p = json.loads(pj.read_text())
    klaar = p.setdefault("gepost", {})
    for stap in volgorde:
        if stap in klaar:
            print(f"  {stap}: al gedaan ({klaar[stap]})")
            continue
        res = doe(stap)
        klaar[stap] = res
        pj.write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n")
        print(f"  {stap}: {res}")
    klaar["tijd"] = nu().isoformat(timespec="seconds")
    pj.write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n")
    return p


def plaats_reel(m):
    tok, ig, page = os.environ["PAGE_TOKEN"], os.environ["IG_USER_ID"], os.environ["PAGE_ID"]
    p = json.loads((m / "post.json").read_text())
    cover = raw_url(m / "cover.jpg")
    wacht_raw([cover])
    with urllib.request.urlopen(video_url(p), timeout=300) as r:   # Release-asset (redirect naar GitHub-opslag)
        data = r.read()
    print(f"  video {p['video']}: {len(data) / 1e6:.1f} MB")
    cap = (m / "caption.txt").read_text().strip()
    versie = GRAPH.rsplit("/", 1)[1]

    def ig_video(**velden):
        c = post(f"{ig}/media", tok, upload_type="resumable", **velden)["id"]
        rupload(f"{RUPLOAD}/ig-api-upload/{versie}/{c}", tok, data)
        wacht_klaar(c, tok, maxs=900)   # video verwerken duurt langer dan een foto
        return post(f"{ig}/media_publish", tok, creation_id=c)["id"]

    def fb_video(eindpunt, **finish):
        r = post(f"{page}/{eindpunt}", tok, upload_phase="start")
        rupload(r.get("upload_url") or f"{RUPLOAD}/video-upload/{versie}/{r['video_id']}", tok, data)
        f = post(f"{page}/{eindpunt}", tok, upload_phase="finish", video_id=r["video_id"], **finish)
        return f.get("post_id") or r["video_id"]

    def doe(stap):
        if stap == "ig_reel":
            return ig_video(media_type="REELS", caption=cap, share_to_feed="true", cover_url=cover)
        if stap == "ig_story":
            return ig_video(media_type="STORIES")
        if stap == "fb_reel":
            return fb_video("video_reels", video_state="PUBLISHED", description=fb_tekst(cap))
        return fb_video("video_stories")

    return stappen(m, ("ig_reel", "ig_story", "fb_reel", "fb_story"), doe)


def plaats(m):
    if soort(m) == "reel":
        return plaats_reel(m)
    tok, ig, page = os.environ["PAGE_TOKEN"], os.environ["IG_USER_ID"], os.environ["PAGE_ID"]
    pj = m / "post.json"
    p = json.loads(pj.read_text())
    klaar = p.setdefault("gepost", {})

    def bewaar(k, v):
        klaar[k] = v
        pj.write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n")

    urls = {n: raw_url(m / n) for n in BEELDEN}
    for u in urls.values():   # raw-cache: even wachten tot de commit-URL antwoordt
        for _ in range(20):
            try:
                with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=15):
                    break
            except urllib.error.HTTPError:
                time.sleep(3)
    cap = (m / "caption.txt").read_text().strip()
    for stap in ("ig_feed", "ig_story", "fb_feed", "fb_story"):
        if stap in klaar:
            print(f"  {stap}: al gedaan ({klaar[stap]})")
            continue
        if stap == "ig_feed":
            kids = [post(f"{ig}/media", tok, image_url=urls[n], is_carousel_item="true")["id"] for n in BEELDEN[:2]]
            for k in kids:
                wacht_klaar(k, tok)
            c = post(f"{ig}/media", tok, media_type="CAROUSEL", children=",".join(kids), caption=cap)["id"]
            wacht_klaar(c, tok)
            res = post(f"{ig}/media_publish", tok, creation_id=c)["id"]
        elif stap == "ig_story":
            c = post(f"{ig}/media", tok, media_type="STORIES", image_url=urls["story.jpg"])["id"]
            wacht_klaar(c, tok)
            res = post(f"{ig}/media_publish", tok, creation_id=c)["id"]
        elif stap == "fb_feed":
            ids = [post(f"{page}/photos", tok, url=urls[n], published="false")["id"] for n in BEELDEN[:2]]
            velden = {f"attached_media[{i}]": json.dumps({"media_fbid": x}) for i, x in enumerate(ids)}
            res = post(f"{page}/feed", tok, message=fb_tekst(cap), **velden)["id"]
        else:
            pid = post(f"{page}/photos", tok, url=urls["story.jpg"], published="false")["id"]
            r = post(f"{page}/photo_stories", tok, photo_id=pid)
            res = r.get("post_id") or r.get("id") or json.dumps(r)
        bewaar(stap, res)
        print(f"  {stap}: {res}")
    bewaar("tijd", nu().isoformat(timespec="seconds"))
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--droog", action="store_true")
    g.add_argument("--rooster", action="store_true")
    g.add_argument("--nu", action="store_true")
    a = ap.parse_args()
    wacht = mappen()
    print(f"{nu():%Y-%m-%d %H:%M} NL, {len(wacht)} in de wachtrij")
    if not wacht:
        sys.exit("wachtrij is leeg: vul hem lokaal met social_queue.py vul --live en push")
    vak = "hand"
    if a.rooster:
        vak = huidig_vak()
        if not vak:
            print("geen tijdvak open, niets te doen")
            return
        if al_gepost(vak):
            print(f"vak {vak} is vandaag al gepost, niets te doen")
            return
    # Deze run: de volgende post plus een reel die er direct op volgt. Staat er een reel vooraan (vorige keer mislukt
    # na de post), dan eerst die reel en daarna alsnog de post van dit vak. Volgorde blijft die van de wachtrij.
    beurt = wacht[:1]
    if len(wacht) > 1 and (soort(wacht[0]) == "reel") != (soort(wacht[1]) == "reel"):
        beurt = wacht[:2]
    for k, m in enumerate(beurt):
        p = json.loads((m / "post.json").read_text())
        print(f"{'volgende' if k == 0 else 'daarna'}: {m.name}, {p['titel']} ({p.get('serie')}), {soort(m)}, vak {vak}")
        if a.droog:
            if soort(m) == "reel":
                print(f"  cover.jpg: {'ok' if (m / 'cover.jpg').exists() else 'ONTBREEKT'}")
                try:
                    with urllib.request.urlopen(urllib.request.Request(video_url(p), method="HEAD"), timeout=30) as r:
                        print(f"  video: ok ({int(r.headers.get('Content-Length', 0)) / 1e6:.1f} MB)")
                except urllib.error.HTTPError as e:
                    print(f"  video: ONTBREEKT ({e.code} op {video_url(p)})")
            else:
                for n in BEELDEN:
                    print(f"  {n}: {'ok' if (m / n).exists() else 'ONTBREEKT'}")
            continue
        if k:
            time.sleep(REEL_PAUZE)
        try:
            plaats(m)
        except Exception as e:   # noqa: BLE001 - loggen, commit bewaart de geslaagde stappen, run faalt zichtbaar
            with LOG.open("a") as f:
                f.write(f"{nu():%Y-%m-%d %H:%M} FOUT [{vak}] {m.name}: {e}\n")
            print(f"FOUT: {e}")
            sys.exit(1)
        GEPOST.mkdir(exist_ok=True)
        shutil.move(str(m), str(GEPOST / m.name))
        with LOG.open("a") as f:
            f.write(f"{nu():%Y-%m-%d %H:%M} OK [{vak}] {m.name} {p['titel']}\n")
        print(f"klaar: {m.name} staat in gepost/")
    if a.droog:
        if os.environ.get("PAGE_TOKEN"):   # secrets testen zonder te posten: alleen lezen
            tok = os.environ["PAGE_TOKEN"]
            ig = get(os.environ["IG_USER_ID"], fields="username", access_token=tok)["username"]
            pg = get(os.environ["PAGE_ID"], fields="name", access_token=tok)["name"]
            print(f"  token ok: Instagram @{ig}, Facebook-pagina {pg}")
        print("  droge run: niets geplaatst")


if __name__ == "__main__":
    main()

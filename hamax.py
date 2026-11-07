"""Forever Addons Manager — gestionnaire local d'addons pour WoW Forever.

Lance un petit serveur sur 127.0.0.1 et ouvre l'interface (ui/index.html)
dans le navigateur. Uniquement la bibliothèque standard de Python.

    python hamax.py                    # démarre et ouvre le navigateur
    python hamax.py --sans-navigateur  # démarre seulement
    python hamax.py --importer-curseforge  # importe les addons de l'appli CurseForge puis quitte

Les mises à jour passent directement par CurseForge (API publique du site) ou
GitHub : l'application CurseForge n'a pas besoin d'être lancée.

Livré en .exe (voir construire.ps1) : icône près de l'horloge, interface dans sa
propre fenêtre (Edge en mode application). Au démarrage de Windows il tourne sans
fenêtre, en priorité basse : aucun calcul tant qu'on ne s'en sert pas, sauf une
vérification des mises à jour toutes les N heures (notification Windows si besoin).
"""
import _winapi
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.7.8"
NOM = "Forever Addons Manager"
DEPOT = "mfarchitecture-ctrl/forever-addons-management"
NOM_INSTALLATEUR = "Forever-Addons-Manager-Setup.exe"
JEU_BATTLENET = "WoWF"   # WoW Forever pour Battle.net --exec (la casse compte)

# Lancé depuis le .exe (PyInstaller) ou depuis le script ?
FIGE = getattr(sys, "frozen", False)
# Ressources livrées avec l'appli (interface, icône, changelog) : dans le .exe, sinon à côté du script.
RESSOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Données (registre, config, sauvegardes) : à part du programme, pour qu'une mise à jour n'y touche jamais.
RACINE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / NOM if FIGE else Path(__file__).resolve().parent
FICHIER_CONFIG = RACINE / "config.json"
FICHIER_FENETRE = RACINE / "fenetre.json"   # taille et position : écrit par le seul processus --fenetre
FICHIER_REGISTRE = RACINE / "registre" / "addons.json"
FICHIER_MAJ = RACINE / "registre" / "maj.json"
DOSSIER_SAUVEGARDES = RACINE / "sauvegardes"
DOSSIER_UI = RESSOURCES / "ui"
FICHIER_CHANGELOG = RESSOURCES / "CHANGELOG.md"

STATUTS = ("a_tester", "valide", "rejete")
# Registres plus anciens. « perso » n'est plus un statut mais une étiquette (champ « perso ») :
# une création peut être à tester, validée ou rejetée comme les autres. La wishlist est fondue
# dans « à tester » : un addon noté mais pas encore installé est simplement « à tester » non installé.
ANCIENS_STATUTS = {"preinstalle": "a_tester", "adopte": "valide", "perso": "a_tester", "wishlist": "a_tester"}
TYPES_SOURCE = ("curseforge", "github", "page", "local", "aucune")
CANAUX = {"release": 1, "beta": 2, "alpha": 3}  # = releaseType CurseForge maximum accepté
SUFFIXE_TOC = re.compile(r"[_-](Mainline|Classic|Vanilla|TBC|BCC|Wrath|WOTLKC|Cata|Mists|Camelot|Standard)$", re.I)
TAILLE_MAX_ZIP = 300 * 1024 * 1024
AGENT = "Mozilla/5.0 (forever-addons-manager)"

verrou = threading.Lock()


class Erreur(Exception):
    """Erreur affichée telle quelle dans l'interface."""


def maintenant():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --- Fichiers JSON ----------------------------------------------------------

def lire_json(chemin, defaut):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaut


def ecrire_json(chemin, donnees):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, chemin)


def migrer_donnees():
    """Versions d'avant la 0.7 : les données étaient à côté du programme, on les range dans le dossier de l'utilisateur."""
    ancien = Path(sys.executable).resolve().parent.parent
    if not FIGE or FICHIER_CONFIG.exists() or not (ancien / "config.json").exists():
        return
    RACINE.mkdir(parents=True, exist_ok=True)
    for nom in ("registre", "sauvegardes", "hamax.log", "config.json"):   # la config en dernier : elle marque la fin
        if (ancien / nom).exists():
            shutil.move(str(ancien / nom), str(RACINE / nom))


def charger_config():
    config = {
        "dossier_addons": "",
        "interface": 16001,
        "curseforge_type_jeu": 88568,
        "curseforge_appli": r"%APPDATA%\CurseForge\agent\GameInstances\AddonGameInstance.json",
        "port": 8765,
        "github_token": "",
        "verification_auto_heures": 12,  # 0 = jamais
        "notifications": False,  # bulles de l'icône près de l'horloge (au nom de l'appli)
        "jeu_confirme": False,   # le dossier du jeu a été trouvé (Analyser) ou choisi (Localiser)
    }
    config.update(lire_json(FICHIER_CONFIG, {}))
    return config


def modifier_config(**valeurs):
    with verrou:
        brut = lire_json(FICHIER_CONFIG, {})
        brut.update(valeurs)
        ecrire_json(FICHIER_CONFIG, brut)


def charger_registre():
    return lire_json(FICHIER_REGISTRE, {"version": 1, "addons": []})


def dossier_valide(config):
    """Vide tant que le jeu n'a pas été trouvé (Path("") serait le dossier courant)."""
    return bool(config["dossier_addons"]) and Path(config["dossier_addons"]).is_dir()


def dossier_addons():
    config = charger_config()
    if not dossier_valide(config):
        raise Erreur("WoW Forever n'est pas encore trouvé : clique sur Analyser sous le titre.")
    return Path(config["dossier_addons"])


# --- Lecture des .toc -------------------------------------------------------

def nettoyer(texte):
    """Retire les codes couleur/texture WoW (|cffxxxxxx … |r, |T…|t)."""
    return re.sub(r"\|c[0-9a-fA-F]{8}|\|r|\|T[^|]*\|t|\|A[^|]*\|a", "", texte).strip()


def lire_toc(chemin):
    meta = {}
    try:
        contenu = chemin.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return meta
    for ligne in contenu.splitlines():
        m = re.match(r"^##\s*([^:]+?)\s*:\s*(.*)$", ligne)
        if m:
            meta.setdefault(m.group(1).strip(), m.group(2).strip())
    return meta


def interfaces(meta):
    return {int(x) for x in re.findall(r"\d+", meta.get("Interface", ""))}


def est_lien(chemin):
    return chemin.is_symlink() or os.path.isjunction(chemin)


def cible_lien(dossier):
    return Path(os.readlink(dossier).removeprefix("\\\\?\\"))


def info_dossier(dossier, interface_cible):
    """Infos d'un dossier d'addon, d'après le .toc qui vise WoW Forever si possible."""
    lien = est_lien(dossier)
    # Un lien se lit directement dans sa cible : selon le contexte, Windows peut refuser de le traverser
    tocs = [(toc, lire_toc(toc)) for toc in sorted((cible_lien(dossier) if lien else dossier).glob("*.toc"))]
    if not tocs:
        return {"titre": dossier.name, "sans_toc": True, "compatible": False, "lien": lien}
    nom = dossier.name
    toc, meta = max(tocs, key=lambda t: (interface_cible in interfaces(t[1]), t[0].stem == nom, t[0].stem.startswith(nom)))
    titre_brut = meta.get("Title", nom)
    version = meta.get("Version", "")
    if not version or "@" in version:
        m = re.search(r"v?\d+(?:\.\d+)+", nettoyer(titre_brut))
        version = m.group(0) if m else ""
    deps = meta.get("Dependencies") or meta.get("RequiredDeps") or meta.get("Dep") or ""
    curse_id = next((m.get("X-Curse-Project-ID") for _, m in tocs if m.get("X-Curse-Project-ID")), "")
    return {
        "titre": nettoyer(titre_brut),
        "notes": nettoyer(meta.get("Notes-frFR") or meta.get("Notes", "")),
        "auteur": meta.get("Author", ""),
        "version": version,
        "toc": toc.name,
        "compatible": any(interface_cible in interfaces(m) for _, m in tocs),
        "dependances": [d.strip() for d in deps.split(",") if d.strip()],
        "site": meta.get("X-Website", ""),
        "curseforge_id": curse_id,
        "lien": lien,
        "cible": os.readlink(dossier) if lien else "",
    }


def scanner(config):
    if not dossier_valide(config):
        return {}
    base = Path(config["dossier_addons"])
    return {
        d.name: info_dossier(d, int(config["interface"]))
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower())
        if d.is_dir() or est_lien(d)
    }


def regrouper(noms, scan):
    """Regroupe les dossiers non répertoriés : un module va avec son addon parent."""
    ensemble = set(noms)

    def parent(nom):
        for dep in scan[nom].get("dependances", []):
            if dep in ensemble and dep != nom:
                return dep
        prefixes = [n for n in ensemble if n != nom and nom.startswith(n)]
        return max(prefixes, key=len) if prefixes else None

    def racine(nom, vus=()):
        p = parent(nom)
        return nom if p is None or p in vus else racine(p, vus + (nom,))

    groupes = {}
    for nom in sorted(noms, key=str.lower):
        groupes.setdefault(racine(nom), []).append(nom)
    return [
        {"dossier": r, "dossiers": [r] + [d for d in membres if d != r], "info": scan[r], "id": identifiant(r)}
        for r, membres in groupes.items()
    ]


def identifiant(texte):
    return re.sub(r"[^a-z0-9]+", "-", str(texte).lower()).strip("-")


# --- Versions ----------------------------------------------------------------

def numeros(v):
    m = re.search(r"\d+(?:\.\d+)+|\d+", v or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def maj_dispo(addon, info, distant):
    """True si une mise à jour est dispo, False sinon, None si on ne sait pas."""
    if not distant or distant.get("erreur"):
        return None
    # Le numéro de version lu dans le .toc fait foi : l'addon a pu être mis à jour hors de l'appli.
    a, b = numeros(info.get("version") if info else ""), numeros(distant.get("version"))
    if a and b:
        return b > a
    installe = addon.get("fichier_installe") or {}
    if installe.get("id") and distant.get("fichier_id"):
        return installe["id"] != distant["fichier_id"]
    return None


# --- État global --------------------------------------------------------------

def reconnaitre(scan):
    """Tout addon présent dans le jeu rejoint la collection tout seul, rangé « à tester ».
    Sans ID CurseForge il n'aura pas de mises à jour, mais il est là. Restent à part les dossiers sans .toc.
    Un dossier relié par un lien est un addon en développement : il va dans « Mes créations »."""
    registre = charger_registre()
    reclames = {d for a in registre["addons"] for d in a.get("dossiers", [])}
    for groupe in regrouper([n for n in scan if n not in reclames], scan):
        info = groupe["info"]
        if info.get("sans_toc"):
            continue
        projet = str(info.get("curseforge_id") or "")
        site = info.get("site", "")
        deja = next((a for a in registre["addons"] if projet.isdigit() and str(a.get("source", {}).get("projet")) == projet), None)
        if deja:
            enregistrer_addon({**deja, "dossiers": deja.get("dossiers", []) + groupe["dossiers"]}, deja["id"])
        else:
            lien = info.get("lien")
            if projet.isdigit():
                source = {"type": "curseforge", "projet": projet, "url": site}
            elif lien:
                source = {"type": "local", "url": info["cible"].removeprefix("\\\\?\\")}
            else:
                source = {"type": "page" if site else "aucune", "url": site}
            enregistrer_addon({
                "nom": info.get("titre") or groupe["dossier"], "statut": "a_tester", "perso": lien,
                "description": info.get("notes", ""), "dossiers": groupe["dossiers"], "source": source,
            })


def etat():
    config = charger_config()
    maj = lire_json(FICHIER_MAJ, {})
    scan = scanner(config)
    reconnaitre(scan)
    registre = charger_registre()
    reclames = set()
    addons = []
    for a in registre["addons"]:
        dossiers = a.get("dossiers", [])
        reclames.update(dossiers)
        trouves = [d for d in dossiers if d in scan]
        info = scan[trouves[0]] if trouves else None
        distant = maj.get(a["id"])
        addons.append({
            **a,
            "installe": bool(trouves),
            "dossiers_trouves": trouves,
            "dossiers_manquants": [d for d in dossiers if d not in scan],
            "incompatibles": [d for d in trouves if not scan[d]["compatible"]],
            "info": info,
            "maj": {**distant, "dispo": maj_dispo(a, info, distant)} if distant else None,
        })
    for a in addons:  # registres plus anciens
        a["perso"] = bool(a.get("perso") or a.get("statut") == "perso")
        a["statut"] = ANCIENS_STATUTS.get(a.get("statut"), a.get("statut"))
    return {
        "config": {k: v for k, v in config.items() if k not in ("github_token", "curseforge_cle")},
        "dossier_ok": dossier_valide(config),
        "addons": addons,
        "inconnus": regrouper([n for n in scan if n not in reclames], scan),
        "demarrage": demarrage_actif(),
        "jeu_confirme": bool(config.get("jeu_confirme")),
        "liens_curseforge": liens_curseforge_actifs(),
        "maj_appli": MAJ_APPLI or None,
        "evenements": [EVENEMENTS.pop(0) for _ in range(len(EVENEMENTS))],
        "version": VERSION,
        "statuts": STATUTS,
        "types_source": TYPES_SOURCE,
    }


# --- Registre -----------------------------------------------------------------

def valider_nom_dossier(nom):
    if not nom or nom in (".", "..") or re.search(r'[\\/:*?"<>|]', nom):
        raise Erreur(f"Nom de dossier invalide : {nom!r}")


def enregistrer_addon(donnees, ancien_id=None):
    src = donnees.get("source") or {}
    a = {
        "id": identifiant(donnees.get("id") or donnees.get("nom") or ""),
        "nom": (donnees.get("nom") or "").strip(),
        "statut": ANCIENS_STATUTS.get(donnees.get("statut"), donnees.get("statut", "a_tester")),
        "perso": bool(donnees.get("perso") or donnees.get("statut") == "perso"),
        "categorie": (donnees.get("categorie") or "").strip(),
        "description": (donnees.get("description") or "").strip(),
        "dossiers": list(dict.fromkeys(d.strip() for d in donnees.get("dossiers", []) if d and d.strip())),
        "source": {"type": src.get("type", "aucune"), "url": (src.get("url") or "").strip()},
        "notes": (donnees.get("notes") or "").strip(),
        "ajoute_le": donnees.get("ajoute_le") or datetime.now().strftime("%Y-%m-%d"),
    }
    if str(src.get("projet") or "").strip():
        if not str(src["projet"]).strip().isdigit():
            raise Erreur("L'ID de projet CurseForge est un nombre (encadré « About Project » sur la page de l'addon).")
        a["source"]["projet"] = int(src["projet"])
    if src.get("motif"):
        a["source"]["motif"] = src["motif"].strip()
    if src.get("canal") and src["canal"] != "release":
        a["source"]["canal"] = src["canal"]
    if donnees.get("fichier_installe"):
        a["fichier_installe"] = donnees["fichier_installe"]
    if str(donnees.get("logo") or "").startswith("https://"):
        a["logo"] = donnees["logo"]

    if not a["id"] or not a["nom"]:
        raise Erreur("Il faut au moins un nom.")
    if a["statut"] not in STATUTS:
        raise Erreur(f"Statut inconnu : {a['statut']}")
    if a["source"]["type"] not in TYPES_SOURCE:
        raise Erreur(f"Type de source inconnu : {a['source']['type']}")
    if a["source"].get("canal", "release") not in CANAUX:
        raise Erreur(f"Canal inconnu : {a['source']['canal']}")
    if a["source"]["type"] == "curseforge" and not a["source"].get("projet"):
        raise Erreur("Pour une source CurseForge, il faut l'ID du projet.")
    for d in a["dossiers"]:
        valider_nom_dossier(d)
    with verrou:
        registre = charger_registre()
        liste = registre["addons"]
        index = next((i for i, x in enumerate(liste) if x["id"] == (ancien_id or a["id"])), None)
        if any(x["id"] == a["id"] for i, x in enumerate(liste) if i != index):
            raise Erreur(f"Un addon avec l'identifiant « {a['id']} » existe déjà.")
        if index is None:
            liste.append(a)
        else:
            a["ajoute_le"] = liste[index].get("ajoute_le", a["ajoute_le"])
            # Clé absente : on garde l'ancien fichier installé. Clé à None : on l'efface.
            if "logo" not in donnees and liste[index].get("logo"):
                a["logo"] = liste[index]["logo"]
            if "fichier_installe" not in donnees and liste[index].get("fichier_installe"):
                a["fichier_installe"] = liste[index]["fichier_installe"]
            liste[index] = a
        ecrire_json(FICHIER_REGISTRE, registre)
    return a


def supprimer_du_registre(addon_id):
    with verrou:
        registre = charger_registre()
        registre["addons"] = [x for x in registre["addons"] if x["id"] != addon_id]
        ecrire_json(FICHIER_REGISTRE, registre)
        maj = lire_json(FICHIER_MAJ, {})
        if maj.pop(addon_id, None) is not None:
            ecrire_json(FICHIER_MAJ, maj)


def trouver_addon(addon_id):
    addon = next((x for x in charger_registre()["addons"] if x["id"] == addon_id), None)
    if not addon:
        raise Erreur(f"Addon inconnu : {addon_id}")
    return addon


# --- Sources distantes (CurseForge, GitHub) -------------------------------------

def requete(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": accept})
    token = charger_config().get("github_token")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        req.add_header("Authorization", f"Bearer {token}")
    hote = urllib.parse.urlparse(url).hostname
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Erreur(f"Introuvable sur {hote} (ID ou adresse faux ?)") from e
        if e.code in (403, 429) and hote == "api.github.com":
            raise Erreur("GitHub limite les requêtes : réessaie dans une heure ou ajoute un github_token dans config.json.") from e
        raise Erreur(f"{hote} a répondu {e.code}") from e
    except urllib.error.URLError as e:
        raise Erreur(f"Pas de connexion à {hote} ({e.reason})") from e


def lire_url_json(url):
    with requete(url) as r:
        return json.load(r)


def derniere_curseforge(projet, canal="release"):
    """Dernier fichier WoW Forever d'un projet CurseForge (API publique du site)."""
    type_jeu = int(charger_config()["curseforge_type_jeu"])
    max_type = CANAUX.get(canal, 1)
    for page in range(6):
        donnees = lire_url_json(
            f"https://www.curseforge.com/api/v1/mods/{int(projet)}/files"
            f"?pageIndex={page}&pageSize=50&sort=dateCreated&sortDescending=true"
        )
        fichiers = donnees.get("data") or []
        for f in fichiers:
            if type_jeu in (f.get("gameVersionTypeIds") or []) and f.get("releaseType", 1) <= max_type:
                return {
                    "version": f.get("displayName") or f.get("fileName", ""),
                    "date": (f.get("dateCreated") or "")[:10],
                    "fichier": f.get("fileName", ""),
                    "fichier_id": f["id"],
                    "taille": f.get("fileLength", 0),
                    "url_zip": f"https://www.curseforge.com/api/v1/mods/{int(projet)}/files/{f['id']}/download",
                    "verifie_le": maintenant(),
                }
        if len(fichiers) < 50:
            break
    raise Erreur("Aucun fichier WoW Forever publié pour cet addon (ou pas dans ce canal).")


def repo_github(url):
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url or "") or re.fullmatch(r"\s*([\w.-]+)/([\w.-]+)\s*", url or "")
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}" if m else None


def derniere_github(source):
    repo = repo_github(source.get("url"))
    if not repo:
        raise Erreur("Adresse GitHub invalide (attendu : https://github.com/auteur/depot).")
    if source.get("canal", "release") != "release":
        release = next((x for x in lire_url_json(f"https://api.github.com/repos/{repo}/releases?per_page=10") if not x.get("draft")), None)
        if not release:
            raise Erreur("Aucune release publiée sur ce dépôt.")
    else:
        release = lire_url_json(f"https://api.github.com/repos/{repo}/releases/latest")
    zips = [x for x in release.get("assets", []) if x["name"].lower().endswith(".zip")]
    if source.get("motif"):
        zips = [x for x in zips if re.search(source["motif"], x["name"], re.I)]
    return {
        "version": release.get("tag_name", ""),
        "date": (release.get("published_at") or "")[:10],
        "fichier": zips[0]["name"] if zips else "",
        "fichier_id": zips[0]["id"] if zips else None,
        "taille": zips[0]["size"] if zips else 0,
        "url_zip": zips[0]["browser_download_url"] if zips else "",
        "page": release.get("html_url", ""),
        "verifie_le": maintenant(),
    }


def derniere_version(addon):
    source = addon.get("source", {})
    if source.get("type") == "curseforge":
        return derniere_curseforge(source.get("projet"), source.get("canal", "release"))
    if source.get("type") == "github":
        return derniere_github(source)
    raise Erreur("Pas de mise à jour automatique pour ce type de source.")


def verifier(ids=None):
    resultats = {}
    for addon in charger_registre()["addons"]:
        if addon.get("source", {}).get("type") not in ("curseforge", "github") or (ids and addon["id"] not in ids):
            continue
        try:
            resultats[addon["id"]] = derniere_version(addon)
        except Erreur as e:
            resultats[addon["id"]] = {"erreur": str(e), "verifie_le": maintenant()}
    with verrou:
        maj = lire_json(FICHIER_MAJ, {})
        maj.update(resultats)
        ecrire_json(FICHIER_MAJ, maj)
    return resultats


PROGRESSION = {}   # [id de l'addon] = {"etape": "telechargement" | "installation", "pourcent": 0-100 ou None, "mo": 1.2}


def telecharger(url, cle=None):
    """Télécharge un zip. Avec « cle », l'avancement est publié dans PROGRESSION pour l'interface."""
    with requete(url, accept="application/octet-stream") as r:
        total = int(r.headers.get("Content-Length") or 0)
        morceaux, recu = [], 0
        while True:
            morceau = r.read(256 * 1024)
            if not morceau:
                break
            morceaux.append(morceau)
            recu += len(morceau)
            if recu > TAILLE_MAX_ZIP:
                raise Erreur("Zip trop gros.")
            if cle:
                PROGRESSION[cle] = {"etape": "telechargement", "pourcent": round(recu * 100 / total) if total else None,
                                    "mo": round(recu / 1048576, 1)}
    return b"".join(morceaux)


def installer_distant(addon_id):
    """Télécharge et installe la dernière version (mise à jour ou première installation)."""
    addon = trouver_addon(addon_id)
    distant = derniere_version(addon)
    if not distant.get("url_zip"):
        raise Erreur("La dernière version n'a pas de fichier .zip (ou aucun ne correspond au motif).")
    try:
        octets = telecharger(distant["url_zip"], addon_id)
        PROGRESSION[addon_id] = {"etape": "installation", "pourcent": 100, "mo": 0}
        resultat = installer_zip(octets, addon_id, fichier=distant)
    finally:
        PROGRESSION.pop(addon_id, None)
    with verrou:
        maj = lire_json(FICHIER_MAJ, {})
        maj[addon_id] = distant
        ecrire_json(FICHIER_MAJ, maj)
    return resultat


def ajouter_curseforge(projet, canal="release"):
    """Installe un addon CurseForge (lien « Install » du site) et le range « à tester »."""
    projet = str(projet).strip()
    if not projet.isdigit():
        raise Erreur("L'ID de projet CurseForge est un nombre.")
    registre = charger_registre()
    deja = next((a for a in registre["addons"] if str(a.get("source", {}).get("projet")) == projet), None)
    if deja:
        # Fiche déjà là (désinstallée, ou à mettre à jour) : on repart d'elle
        enregistrer_addon({**deja, "source": {"type": "curseforge", "projet": int(projet),
                                              "url": deja.get("source", {}).get("url", ""), "canal": canal}}, deja["id"])
        return installer_distant(deja["id"])
    distant = derniere_curseforge(projet, canal)
    source = {"type": "curseforge", "projet": int(projet), "url": "", "canal": canal}
    resultat = installer_zip(telecharger(distant["url_zip"]), fichier=distant, source=source)
    with verrou:
        maj = lire_json(FICHIER_MAJ, {})
        maj[resultat["id"]] = distant
        ecrire_json(FICHIER_MAJ, maj)
    return resultat


# --- Dossier du jeu : analyser (trouver tout seul) ou localiser (choisir) --------------

def _dossier_addons_depuis(chemin):
    """Ramène n'importe quel dossier choisi (WoW, _forever_, Interface, AddOns) au dossier AddOns de Forever."""
    p = Path(chemin)
    for candidat in (p, p / "AddOns", p / "Interface" / "AddOns", p / "_forever_" / "Interface" / "AddOns"):
        if candidat.name.lower() == "addons" and candidat.is_dir():
            return candidat
    if (p / "_forever_").is_dir() or p.name.lower() == "_forever_":
        racine = p if p.name.lower() == "_forever_" else p / "_forever_"
        addons = racine / "Interface" / "AddOns"
        addons.mkdir(parents=True, exist_ok=True)   # jeu jamais lancé avec des addons : le dossier n'existe pas encore
        return addons
    return None


def analyser_jeu():
    """Cherche les installations de WoW Forever : appli CurseForge, Battle.net, emplacements habituels."""
    racines = set()
    try:  # installations connues de l'appli CurseForge
        for inst in lire_json(Path(os.path.expandvars(charger_config()["curseforge_appli"])), []):
            if inst.get("installPath"):
                racines.add(Path(inst["installPath"]).parent)
    except (OSError, ValueError):
        pass
    try:  # Battle.net garde ses chemins d'installation en clair dans product.db
        db = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Battle.net" / "Agent" / "product.db"
        for m in re.finditer(rb"[A-Za-z]:[\\/][^\x00-\x1f\"]{0,200}?World of Warcraft", db.read_bytes()):
            racines.add(Path(m.group(0).decode("utf-8", "ignore")))
    except OSError:
        pass
    for lettre in "CDEFGHIJ":
        for base in ("", "Program Files (x86)", "Program Files", "Games", "Jeux", "Battle.net", "Blizzard"):
            racines.add(Path(f"{lettre}:\\") / base / "World of Warcraft")
    trouves = []
    for r in sorted(racines, key=str):
        forever = r / "_forever_"
        if forever.is_dir() and (forever / "Wow.exe").exists():
            trouves.append(str(forever / "Interface" / "AddOns"))
    return {"trouves": list(dict.fromkeys(trouves))}


def choisir_jeu(chemin):
    addons = _dossier_addons_depuis(chemin)
    if not addons:
        raise Erreur("Ce dossier ne contient pas WoW Forever (on attend « World of Warcraft », « _forever_ » ou « AddOns »).")
    modifier_config(dossier_addons=str(addons), jeu_confirme=True)
    return {"dossier_addons": str(addons)}


def choisir_dossier(consigne, depart=None):
    """Fenêtre Windows moderne « Sélectionner un dossier » (IFileOpenDialog), qui suit le thème de Windows.
    Renvoie le dossier choisi, ou "" si annulé."""
    import ctypes
    from ctypes import wintypes
    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32

    def guid(texte):
        g = ctypes.create_string_buffer(16)
        ole32.CLSIDFromString(texte, g)
        return g

    def methode(objet, index, *arguments):
        """La méthode n° index de la table d'un objet COM (voir shobjidl_core.h)."""
        table = ctypes.cast(objet, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *arguments)(table[index])

    OUVRIR, IID_DIALOGUE = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}", "{D57C7288-D4AD-4768-BE02-9D969532D960}"
    IID_ELEMENT = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
    ole32.CLSIDFromString.argtypes = (wintypes.LPCWSTR, ctypes.c_char_p)
    ole32.CoCreateInstance.argtypes = (ctypes.c_char_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    shell32.SHCreateItemFromParsingName.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
    ole32.CoInitializeEx(None, 2)                     # COINIT_APARTMENTTHREADED

    dialogue = ctypes.c_void_p()
    if ole32.CoCreateInstance(guid(OUVRIR), None, 1, guid(IID_DIALOGUE), ctypes.byref(dialogue)) != 0:
        raise Erreur("Impossible d'ouvrir la fenêtre de choix de dossier.")
    try:
        options = wintypes.DWORD()
        methode(dialogue, 10, ctypes.POINTER(wintypes.DWORD))(dialogue, ctypes.byref(options))        # GetOptions
        methode(dialogue, 9, wintypes.DWORD)(dialogue, options.value | 0x20 | 0x40)                   # SetOptions : dossiers, vrais fichiers
        methode(dialogue, 17, wintypes.LPCWSTR)(dialogue, consigne)                                   # SetTitle
        if depart and Path(depart).is_dir():
            element = ctypes.c_void_p()
            if shell32.SHCreateItemFromParsingName(str(depart), None, guid(IID_ELEMENT), ctypes.byref(element)) == 0:
                methode(dialogue, 12, ctypes.c_void_p)(dialogue, element)                             # SetFolder
                methode(element, 2)(element)                                                          # Release
        if methode(dialogue, 3, wintypes.HWND)(dialogue, trouver_fenetre()) != 0:                     # Show : annulé
            return ""
        resultat = ctypes.c_void_p()
        if methode(dialogue, 20, ctypes.POINTER(ctypes.c_void_p))(dialogue, ctypes.byref(resultat)) != 0:   # GetResult
            return ""
        texte = ctypes.c_void_p()
        chemin = ""
        # GetDisplayName(SIGDN_FILESYSPATH)
        if methode(resultat, 5, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(resultat, 0x80058000, ctypes.byref(texte)) == 0:
            chemin = ctypes.wstring_at(texte)
            ole32.CoTaskMemFree(texte)
        methode(resultat, 2)(resultat)
        return chemin
    finally:
        methode(dialogue, 2)(dialogue)


def parcourir_jeu():
    chemin = choisir_dossier("Choisis le dossier World of Warcraft (ou _forever_)", charger_config().get("dossier_addons"))
    return choisir_jeu(chemin) if chemin else {"annule": True}


def ajouter_creation():
    """Un addon en développement : fiche « Ma création », et lien du dossier vers le jeu."""
    chemin = choisir_dossier("Choisis le dossier de ton addon (celui qui contient le fichier .toc)")
    if not chemin:
        return {"annule": True}
    dossier = Path(chemin)
    tocs = sorted(dossier.glob("*.toc"))
    if not tocs:
        raise Erreur(f"Pas de fichier .toc dans {dossier} : ce n'est pas un dossier d'addon.")
    if any(Path(a.get("source", {}).get("url") or ".") == dossier for a in charger_registre()["addons"]):
        raise Erreur(f"Cet addon est déjà dans ta collection : {dossier.name}")
    meta = lire_toc(next((t for t in tocs if t.stem == dossier.name), tocs[0]))
    addon = enregistrer_addon({
        "nom": nettoyer(meta.get("Title", dossier.name)) or dossier.name,
        "statut": "a_tester", "perso": True,
        "description": nettoyer(meta.get("Notes-frFR") or meta.get("Notes", "")),
        "source": {"type": "local", "url": str(dossier)},
    })
    lier(addon["id"])
    return addon


# --- Import depuis l'appli CurseForge (lecture seule) ---------------------------

def importer_curseforge():
    """Relie le registre aux addons installés par l'appli CurseForge (ID de projet, fichier installé)."""
    config = charger_config()
    chemin = Path(os.path.expandvars(config["curseforge_appli"]))
    if not chemin.is_file():
        raise Erreur(f"Fichier de l'appli CurseForge introuvable : {chemin}")
    jeu = Path(config["dossier_addons"]).resolve().parent.parent
    instance = next(
        (i for i in lire_json(chemin, []) if Path(i.get("installPath", "")).resolve() == jeu),
        None,
    )
    if not instance:
        raise Erreur(f"L'appli CurseForge ne connaît pas d'installation dans {jeu}")
    relies, ajoutes = [], []
    for cf in instance.get("installedAddons", []):
        fichier = cf.get("installedFile") or {}
        modules = [m["foldername"] for m in fichier.get("modules", []) if m.get("foldername")]
        installe = {"id": fichier.get("id"), "nom": fichier.get("fileName", ""), "date": (fichier.get("fileDate") or "")[:10]}
        source = {"type": "curseforge", "projet": cf["addonID"], "url": cf.get("webSiteURL") or ""}
        logo = cf.get("thumbnailUrl") or ""
        # Même projet, ou même dossier principal. Pas un simple dossier en commun : QuestieDB est un
        # projet à part, il ne doit pas être rattaché à Questie qui l'utilise.
        registre = charger_registre()
        addon = next((a for a in registre["addons"] if str(a.get("source", {}).get("projet")) == str(cf["addonID"])), None) \
            or next((a for a in registre["addons"] if a.get("dossiers") and a["dossiers"][0] in modules), None)
        if addon:
            canal = addon.get("source", {}).get("canal")
            enregistrer_addon({
                **addon,
                "dossiers": addon.get("dossiers", []) + modules,
                "source": {**source, "canal": canal} if canal else source,
                "fichier_installe": installe,
                "logo": logo or addon.get("logo", ""),
            }, addon["id"])
            relies.append(addon["nom"])
        else:
            # Ses dossiers ne doivent plus être comptés chez un autre addon
            for autre in registre["addons"]:
                restants = [d for d in autre.get("dossiers", []) if d not in modules]
                if restants != autre.get("dossiers", []):
                    enregistrer_addon({**autre, "dossiers": restants}, autre["id"])
            enregistrer_addon({
                "nom": cf.get("name") or modules[0],
                "statut": "a_tester",
                "dossiers": modules,
                "source": source,
                "fichier_installe": installe,
                "logo": logo,
            })
            ajoutes.append(cf.get("name"))
    return {"relies": relies, "ajoutes": ajoutes}


# --- Installation / retrait ------------------------------------------------------

def dossiers_du_zip(zf):
    """{chemin dans le zip: nom du dossier à créer dans AddOns}, un par addon trouvé."""
    chemins = [n.replace("\\", "/") for n in zf.namelist()]
    for p in chemins:
        morceaux = p.split("/")
        if p.startswith("/") or ":" in morceaux[0] or ".." in morceaux:
            raise Erreur(f"Zip refusé : chemin suspect « {p} »")
    racines = {}
    for p in chemins:
        if p.lower().endswith(".toc"):
            dossier, _, fichier = p.rpartition("/")
            racines.setdefault(dossier, []).append(Path(fichier).stem)

    def imbrique(d):
        return any(autre != d and (autre == "" or d.startswith(autre + "/")) for autre in racines)

    resultat = {}
    for dossier, stems in racines.items():
        if imbrique(dossier):
            continue
        base = dossier.rpartition("/")[2]
        bases_toc = [SUFFIXE_TOC.sub("", s) for s in stems]
        resultat[dossier] = base if base in stems or base in bases_toc else min(bases_toc, key=len)
    return resultat


# Une installation à la fois : deux lots lancés ensemble ne doivent jamais se marcher dessus.
verrou_installation = threading.Lock()


def installer_zip(octets, addon_id=None, nom_fichier="", fichier=None, source=None):
    """Installe les addons d'un zip. Les dossiers remplacés partent dans sauvegardes/."""
    with verrou_installation:
        return _installer_zip(octets, addon_id, nom_fichier, fichier, source)


def _installer_zip(octets, addon_id, nom_fichier, fichier, source):
    base = dossier_addons()
    try:
        zf = zipfile.ZipFile(io.BytesIO(octets))
    except zipfile.BadZipFile as e:
        raise Erreur("Ce fichier n'est pas un zip valide.") from e
    racines = dossiers_du_zip(zf)
    if not racines:
        raise Erreur("Aucun addon dans ce zip (pas de fichier .toc).")
    noms = list(dict.fromkeys(racines.values()))
    for nom in noms:
        valider_nom_dossier(nom)
        if est_lien(base / nom):
            raise Erreur(f"« {nom} » est un lien vers un addon perso : je n'y touche pas.")

    horodatage = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    # Dossier propre à cette installation : un nom à la seconde près serait partagé, et le premier
    # à finir effacerait les fichiers de l'autre.
    (DOSSIER_SAUVEGARDES / "_tmp").mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=horodatage + "_", dir=DOSSIER_SAUVEGARDES / "_tmp"))
    try:
        for info in zf.infolist():
            p = info.filename.replace("\\", "/")
            for racine, nom in racines.items():
                prefixe = racine + "/" if racine else ""
                if not p.startswith(prefixe) or not p[len(prefixe):]:
                    continue
                cible = tmp / nom / p[len(prefixe):]
                if info.is_dir():
                    cible.mkdir(parents=True, exist_ok=True)
                else:
                    cible.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(cible, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                break

        # Rattacher au registre : addon demandé, sinon celui qui possède déjà ces dossiers, sinon nouvel addon « à tester ».
        registre = charger_registre()
        if addon_id:
            addon = trouver_addon(addon_id)
        else:
            addon = next((a for a in registre["addons"] if set(a.get("dossiers", [])) & set(noms)), None)
        if addon is None:
            info = info_dossier(tmp / noms[0], int(charger_config()["interface"]))
            addon = {
                "id": identifiant(info.get("titre") or noms[0]),
                "nom": info.get("titre") or noms[0],
                "statut": "a_tester",
                "description": info.get("notes", ""),
                "dossiers": [],
                "source": source or ({"type": "page", "url": info.get("site", "")} if info.get("site") else {"type": "aucune", "url": ""}),
                "notes": f"Installé depuis {nom_fichier}" if nom_fichier else "",
            }
            if not source and info.get("curseforge_id", "").isdigit():
                addon["source"] = {"type": "curseforge", "projet": int(info["curseforge_id"]), "url": info.get("site", "")}
            while any(a["id"] == addon["id"] for a in registre["addons"]):
                addon["id"] += "-2"
            addon_id_final = None
        else:
            addon_id_final = addon["id"]

        sauvegarde = DOSSIER_SAUVEGARDES / addon["id"] / horodatage
        rang = 2
        while sauvegarde.exists():   # déjà une sauvegarde de cet addon dans la même seconde
            sauvegarde = DOSSIER_SAUVEGARDES / addon["id"] / f"{horodatage}_{rang}"
            rang += 1
        remplaces = []
        for nom in noms:
            dest = base / nom
            if dest.exists():
                sauvegarde.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest), str(sauvegarde / nom))
                remplaces.append(nom)
            shutil.move(str(tmp / nom), str(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    addon = enregistrer_addon({
        **addon,
        "statut": addon.get("statut", "a_tester"),
        "dossiers": addon.get("dossiers", []) + noms,
        # Zip déposé à la main : on ne sait plus quel fichier distant c'est, la comparaison se fera sur la version.
        "fichier_installe": {"id": fichier["fichier_id"], "nom": fichier.get("fichier", ""), "date": fichier.get("date", "")}
        if fichier and fichier.get("fichier_id") else None,
    }, addon_id_final)
    return {
        "id": addon["id"],
        "nom": addon["nom"],
        "dossiers": noms,
        "remplaces": remplaces,
        "sauvegarde": str(sauvegarde) if remplaces else "",
    }


def retirer(addon_id):
    """Sort les dossiers de l'addon du jeu (vers sauvegardes/). Un lien perso est juste détaché."""
    addon = trouver_addon(addon_id)
    base = dossier_addons()
    sauvegarde = DOSSIER_SAUVEGARDES / addon_id / (datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_retire")
    deplaces, detaches = [], []
    for nom in addon.get("dossiers", []):
        valider_nom_dossier(nom)
        dest = base / nom
        if est_lien(dest):
            os.rmdir(dest)  # supprime le lien uniquement, jamais le contenu de la cible
            detaches.append(nom)
        elif dest.exists():
            sauvegarde.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(sauvegarde / nom))
            deplaces.append(nom)
    return {"deplaces": deplaces, "detaches": detaches, "sauvegarde": str(sauvegarde) if deplaces else ""}


def lier(addon_id):
    """Crée un lien (junction) AddOns/<dossier> -> dossier de dev d'un addon perso."""
    addon = trouver_addon(addon_id)
    source = Path(addon.get("source", {}).get("url", ""))
    if addon.get("source", {}).get("type") != "local" or not source.is_dir():
        raise Erreur("Il faut une source « local » qui pointe vers le dossier de l'addon.")
    if not list(source.glob("*.toc")):
        raise Erreur(f"Pas de fichier .toc dans {source}")
    dest = dossier_addons() / source.name
    if dest.exists() or est_lien(dest):
        raise Erreur(f"Il existe déjà un dossier {source.name} dans AddOns.")
    try:  # appel direct, sans passer par cmd : un « & » dans le nom du dossier ne doit rien exécuter
        _winapi.CreateJunction(str(source), str(dest))
    except OSError as e:
        raise Erreur(f"Échec du lien : {e}") from e
    if source.name not in addon.get("dossiers", []):
        enregistrer_addon({**addon, "dossiers": addon.get("dossiers", []) + [source.name]}, addon_id)
    return {"lien": str(dest), "cible": str(source)}


def ouvrir(addon_id):
    addon = trouver_addon(addon_id)
    base = dossier_addons()
    dossier = next((base / d for d in addon.get("dossiers", []) if (base / d).exists()), base)
    os.startfile(dossier)
    return {"ouvert": str(dossier)}


# --- Lancer le jeu ---------------------------------------------------------------------

def _battlenet():
    """Battle.net.exe, d'après le protocole battlenet:// qu'il inscrit dans Windows ; None s'il n'y est pas."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"battlenet\shell\open\command") as cle:
            commande = winreg.QueryValue(cle, None)
    except OSError:
        return None
    m = re.match(r'"([^"]+)"', commande)
    return Path(m.group(1)) if m and Path(m.group(1)).is_file() else None


def jouer():
    """Passe par Battle.net, seul à connecter le compte : Wow.exe seul demande les identifiants."""
    forever = dossier_addons().parent.parent
    wow = forever / "Wow.exe"
    if not wow.is_file():
        raise Erreur(f"Wow.exe est introuvable dans {forever}.")
    battlenet = _battlenet()
    if battlenet:
        subprocess.Popen(f'"{battlenet}" --exec="launch {JEU_BATTLENET}"', cwd=str(battlenet.parent), close_fds=True)
    else:
        subprocess.Popen([str(wow)], cwd=str(forever), close_fds=True)
    return {}


# --- Démarrage avec Windows, priorité, notifications -----------------------------

CLE_DEMARRAGE = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALEUR_DEMARRAGE = "ForeverAddonsManager"
FICHIER_JOURNAL = RACINE / "hamax.log"


def cible_lancement():
    """Programme à lancer et ses arguments : le .exe, ou pythonw + hamax.py en développement."""
    if FIGE:
        return Path(sys.executable), ""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return (pyw if pyw.exists() else exe), f'"{RACINE / "hamax.py"}"'


def commande_lancement(*arguments):
    cible, base = cible_lancement()
    return " ".join(x for x in (f'"{cible}"', base, *arguments) if x)


def demarrage_actif():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_DEMARRAGE) as k:
            return bool(winreg.QueryValueEx(k, VALEUR_DEMARRAGE)[0])
    except OSError:
        return False


def regler_demarrage(actif):
    """Lancement avec Windows : une simple entrée de l'utilisateur, sans droits administrateur."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLE_DEMARRAGE) as k:
        if actif:
            winreg.SetValueEx(k, VALEUR_DEMARRAGE, 0, winreg.REG_SZ, commande_lancement("--demarrage"))
        else:
            try:
                winreg.DeleteValue(k, VALEUR_DEMARRAGE)
            except OSError:
                pass
    return {"demarrage": demarrage_actif()}


def priorite_arriere_plan():
    """Priorité CPU abaissée : ne gêne jamais le jeu."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE  # sinon le handle 64 bits est tronqué
        k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        moi = k32.GetCurrentProcess()
        # Au repos l'appli ne calcule rien : la priorité ne joue que quand on s'en sert.
        # « Inactive » ou le mode arrière-plan rendent l'interface lente (2 s par page) : « inférieure à la normale » suffit.
        k32.SetPriorityClass(moi, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
    except (AttributeError, OSError):
        pass


def notifier(titre, texte):
    """Bulle de l'icône près de l'horloge : Windows l'affiche au nom de l'appli.
    Désactivé par défaut (« notifications » dans config.json)."""
    if not charger_config().get("notifications") or ICONE is None:
        return
    try:
        ICONE.notify(texte, titre)
    except Exception as e:  # noqa: BLE001 — une bulle ratée n'est pas grave
        print(f"{maintenant()} notification : {e}")


# --- Mise à jour de l'appli (GitHub Releases) -------------------------------------

MAJ_APPLI = {}   # la version plus récente trouvée sur GitHub : version, url, taille, empreinte, notes


def numero_version(texte):
    return tuple(int(x) for x in re.findall(r"\d+", texte)[:3])


def lire_release(release):
    """La mise à jour décrite par une release GitHub, ou None si elle n'apporte rien ou n'est pas exploitable."""
    version = str(release.get("tag_name", "")).lstrip("v")
    if release.get("draft") or release.get("prerelease") or not version or numero_version(version) <= numero_version(VERSION):
        return None
    prefixe = f"https://github.com/{DEPOT}/releases/download/"
    for fichier in release.get("assets", []):
        url = str(fichier.get("browser_download_url", ""))
        if fichier.get("name") == NOM_INSTALLATEUR and url.startswith(prefixe):
            return {"version": version, "url": url, "taille": fichier.get("size", 0),
                    "empreinte": fichier.get("digest", ""), "notes": release.get("body", "")}
    return None


def chercher_maj_appli():
    """Une seule requête vers GitHub ; rien sur l'utilisateur ou le PC n'est envoyé. Un échec reste silencieux."""
    if not FIGE:
        return
    try:
        with requete(f"https://api.github.com/repos/{DEPOT}/releases/latest", accept="application/vnd.github+json") as r:
            maj = lire_release(json.load(r))
    except (Erreur, OSError, ValueError) as e:
        print(f"{maintenant()} mise à jour de l'appli : {e}")
        return False
    MAJ_APPLI.clear()
    MAJ_APPLI.update(maj or {})
    return True


def verifier_appli():
    if chercher_maj_appli() is False:
        raise Erreur("Impossible de joindre GitHub pour le moment. Réessaie plus tard.")
    return {"maj": MAJ_APPLI or None}


def controler_installateur(octets, maj):
    """L'installateur doit être un programme Windows, de la taille et de l'empreinte annoncées par GitHub."""
    if octets[:2] != b"MZ" or (maj["taille"] and len(octets) != maj["taille"]):
        raise Erreur("Le fichier téléchargé est incomplet : mise à jour annulée.")
    empreinte = maj["empreinte"]
    if empreinte and empreinte.lower() != "sha256:" + hashlib.sha256(octets).hexdigest():
        raise Erreur("Le fichier téléchargé ne correspond pas à la version publiée : mise à jour annulée.")


def installer_maj_appli():
    """Télécharge l'installateur, le lance en silencieux et quitte : il remplace le programme puis relance l'appli."""
    if not MAJ_APPLI:
        raise Erreur("Aucune mise à jour de l'appli disponible.")
    maj = dict(MAJ_APPLI)
    try:
        octets = telecharger(maj["url"], "appli")
        controler_installateur(octets, maj)
    finally:
        PROGRESSION.pop("appli", None)
    dossier = RACINE / "maj"
    shutil.rmtree(dossier, ignore_errors=True)
    dossier.mkdir(parents=True)
    installateur = dossier / NOM_INSTALLATEUR
    installateur.write_bytes(octets)
    # Sans cette variable, l'appli relancée par l'installateur se croirait l'enfant de celle-ci
    subprocess.Popen([str(installateur), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/MAJ=1"], close_fds=True,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    threading.Timer(0.5, demander_arret).start()
    return {"version": maj["version"]}


def verifier_tout(ids=None):
    chercher_maj_appli()
    return verifier(ids)


def verifier_et_signaler(deja_signale=None):
    """Vérifie tout et notifie. Avec deja_signale : seulement les mises à jour pas encore annoncées."""
    verifier_tout()
    dispo = [a for a in etat()["addons"] if a["installe"] and (a.get("maj") or {}).get("dispo")]
    if deja_signale is None:
        texte = f"{len(dispo)} mise(s) à jour : " + ", ".join(a["nom"] for a in dispo) if dispo else "Tous tes addons sont à jour."
        notifier(NOM, texte)
        return dispo
    nouveaux = {(a["id"], a["maj"].get("version")) for a in dispo} - deja_signale
    if nouveaux:
        deja_signale |= nouveaux
        notifier(NOM, f"{len(dispo)} mise(s) à jour d'addons : " + ", ".join(a["nom"] for a in dispo))
    return dispo


def verification_auto(arret):
    """Vérifie les mises à jour au démarrage (après 2 min) puis toutes les N heures. Dort entre-temps."""
    heures = float(charger_config().get("verification_auto_heures") or 0)
    if heures <= 0:
        return
    deja_signale = set()
    attente = 120
    while not arret.wait(attente):
        attente = heures * 3600
        try:
            verifier_et_signaler(deja_signale)
        except Exception as e:  # noqa: BLE001 — une vérification ratée ne doit pas arrêter le service
            print(f"{maintenant()} vérification auto : {e}")


# --- Fenêtre et icône de la zone de notification --------------------------------

def adresse_locale():
    return f"http://127.0.0.1:{int(charger_config()['port'])}/"


def chercher_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            exe = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if exe.exists():
                return exe
    return None


def trouver_fenetre():
    """La fenêtre de l'appli, trouvée par son titre (elle vit dans un autre processus). None si fermée."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return None
    trouvees = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visiter(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            titre = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, titre, n + 1)
            if titre.value == NOM:
                trouvees.append(hwnd)
                return False
        return True

    user32.EnumWindows(visiter, 0)
    return trouvees[0] if trouvees else None


def ramener_fenetre():
    """Si la fenêtre de l'appli est déjà ouverte, la remet au premier plan. Renvoie True si trouvée."""
    hwnd = trouver_fenetre()
    if not hwnd:
        return False
    import ctypes
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


TAILLE_MINI = (960, 640)  # en pixels à 100 % d'échelle Windows


def surveiller_taille_fenetre():
    """Taille minimale de la fenêtre. Edge ne laisse pas une page l'imposer : on écoute la fin de chaque
    redimensionnement (événement Windows, aucun calcul entre-temps) et on agrandit la fenêtre de l'appli si besoin."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return

    def titre(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    @ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                        wintypes.DWORD, wintypes.DWORD)
    def fin_redimensionnement(_hook, _evt, hwnd, id_objet, _enfant, _fil, _temps):
        if id_objet != 0 or not hwnd or titre(hwnd) != NOM:
            return
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        echelle = (user32.GetDpiForWindow(hwnd) or 96) / 96
        mini_l, mini_h = round(TAILLE_MINI[0] * echelle), round(TAILLE_MINI[1] * echelle)
        largeur, hauteur = rect.right - rect.left, rect.bottom - rect.top
        if largeur < mini_l or hauteur < mini_h:
            # SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, None, 0, 0, max(largeur, mini_l), max(hauteur, mini_h), 0x0002 | 0x0004 | 0x0010)

    user32.SetWinEventHook.restype = wintypes.HANDLE
    # EVENT_SYSTEM_MOVESIZEEND, hors contexte : Windows nous prévient, on ne touche pas au processus d'Edge
    if not user32.SetWinEventHook(0x000B, 0x000B, None, fin_redimensionnement, 0, 0, 0x0000):
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:   # dort jusqu'au prochain événement
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


FENETRE = None                          # dans le processus --fenetre uniquement
HWND_FENETRE = None
_processus_fenetre = None
_verrou_processus = threading.Lock()
_arret_demande = threading.Event()
DOSSIER_WEBVIEW = Path(os.environ.get("LOCALAPPDATA", str(RACINE))) / NOM / "WebView2"   # réglages de la page (compact…)


def moteur_fenetre_disponible():
    import importlib.util
    return importlib.util.find_spec("webview") is not None


def ouvrir_interface():
    """Ouvre ou ramène l'unique fenêtre de l'appli.
    Elle vit dans un processus à part (« --fenetre ») qui s'arrête entièrement à la fermeture : le moteur web,
    .NET et leur mémoire disparaissent avec lui, et il ne reste que le petit programme près de l'horloge."""
    global _processus_fenetre
    if not moteur_fenetre_disponible():
        return ouvrir_edge()
    import time
    with _verrou_processus:
        if _processus_fenetre is not None and _processus_fenetre.poll() is None:
            for _ in range(40):               # elle peut être en train de s'ouvrir : jusqu'à 8 s
                if ramener_fenetre():
                    return
                time.sleep(0.2)
            return
        cible, _base = cible_lancement()
        commande = [str(cible)] + ([] if FIGE else [str(RACINE / "hamax.py")]) + ["--fenetre"]
        _processus_fenetre = subprocess.Popen(commande, cwd=str(RACINE))


def _apres_affichage():
    """Barre de titre aux couleurs de l'appli (Windows 11) et fenêtre au premier plan."""
    global HWND_FENETRE
    try:
        import ctypes
        from ctypes import wintypes
        HWND_FENETRE = int(FENETRE.native.Handle.ToInt64())
        dwm = ctypes.windll.dwmapi
        for attribut, valeur in ((20, 1),               # DWMWA_USE_IMMERSIVE_DARK_MODE
                                 (35, 0x000A0D0F),      # DWMWA_CAPTION_COLOR : #0f0d0a (format BGR)
                                 (36, 0x00D6E4EC),      # DWMWA_TEXT_COLOR : #ece4d6
                                 (34, 0x000A0D0F)):     # DWMWA_BORDER_COLOR
            v = wintypes.DWORD(valeur)
            dwm.DwmSetWindowAttribute(wintypes.HWND(HWND_FENETRE), attribut, ctypes.byref(v), ctypes.sizeof(v))
        ctypes.windll.user32.SetForegroundWindow(HWND_FENETRE)
    except Exception as e:  # noqa: BLE001 — une barre de titre aux couleurs Windows n'est pas grave
        print(f"{maintenant()} barre de titre : {e}")
    try:
        _suivre_echelle(HWND_FENETRE)
    except Exception as e:  # noqa: BLE001
        print(f"{maintenant()} suivi de l'échelle : {e}")


def _suivre_echelle(hwnd):
    """Garde la fenêtre utilisable quand elle change d'écran (ex. 4K à 200 % vers 1080p à 100 %).
    pywebview calcule la taille et le minimum en pixels réels une seule fois, pour l'écran de départ :
    960 × 640 à 200 % donne un minimum de 1920 × 1280, plus haut qu'un écran 1080p, donc impossible à réduire.
    Ici on les recalcule à chaque changement d'échelle, sans jamais dépasser la zone utile de l'écran."""
    import ctypes
    from ctypes import wintypes

    from System import Action
    from System.Drawing import Size
    from System.Windows.Forms import FormWindowState

    class InfosEcran(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u32 = ctypes.windll.user32
    u32.MonitorFromWindow.restype = wintypes.HMONITOR
    u32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    u32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.POINTER(InfosEcran))
    form = FENETRE.native
    memoire = {"dpi": u32.GetDpiForWindow(hwnd)}

    def zone_utile():
        infos = InfosEcran()
        infos.cbSize = ctypes.sizeof(InfosEcran)
        u32.GetMonitorInfoW(u32.MonitorFromWindow(hwnd, 2), ctypes.byref(infos))   # 2 : écran le plus proche
        return infos.rcWork.right - infos.rcWork.left, infos.rcWork.bottom - infos.rcWork.top

    def ajuster(forcer=False):
        dpi = u32.GetDpiForWindow(hwnd)
        ancien = memoire["dpi"]
        if dpi == ancien and not forcer:
            return
        memoire["dpi"] = dpi
        largeur_max, hauteur_max = zone_utile()
        largeur, hauteur = form.Width, form.Height   # lues avant : un minimum plus grand agrandit la fenêtre
        # Le minimum d'abord : tant qu'il est trop grand, Windows refuserait de réduire la fenêtre
        form.MinimumSize = Size(min(round(TAILLE_MINI[0] * dpi / 96), largeur_max),
                                min(round(TAILLE_MINI[1] * dpi / 96), hauteur_max))
        if form.WindowState == FormWindowState.Normal:
            form.Size = Size(min(round(largeur * dpi / ancien), largeur_max),
                             min(round(hauteur * dpi / ancien), hauteur_max))

    def verifier():
        try:
            ajuster()
        except Exception as e:  # noqa: BLE001
            print(f"{maintenant()} changement d'écran : {e}")

    def au_deplacement(_expediteur, _arguments):
        # Windows annonce le déplacement avant la nouvelle échelle : on lit celle-ci juste après.
        # Coûte une lecture du DPI, et seulement quand la fenêtre bouge.
        form.BeginInvoke(Action(verifier))

    form.Move += au_deplacement
    form.BeginInvoke(Action(lambda: ajuster(True)))   # au cas où elle s'ouvre déjà sur un autre écran


class PlacementFenetre(ctypes.Structure):
    """WINDOWPLACEMENT de Windows : position hors agrandissement et état de la fenêtre."""
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                ("ptMinPosition", ctypes.c_long * 2), ("ptMaxPosition", ctypes.c_long * 2),
                ("rcNormalPosition", ctypes.c_long * 4)]


_placement_ecrit = None


def _enregistrer_placement(*_):
    """Note la taille, la position et l'agrandissement de la fenêtre, en pixels réels.
    Appelé à chaque fin de déplacement, pas seulement à la fermeture : « Quitter » arrête la fenêtre net."""
    global _placement_ecrit
    try:
        from ctypes import wintypes
        p = PlacementFenetre(length=ctypes.sizeof(PlacementFenetre))
        hwnd = wintypes.HWND(int(FENETRE.native.Handle.ToInt64()))
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(p)):
            return
        # Réduite dans la barre des tâches, showCmd vaut 2 et flags dit si elle était agrandie avant
        placement = {"rect": list(p.rcNormalPosition), "agrandie": p.showCmd == 3 or (p.showCmd == 2 and bool(p.flags & 2))}
        with verrou:
            if placement != _placement_ecrit:
                ecrire_json(FICHIER_FENETRE, placement)
                _placement_ecrit = placement
    except Exception as e:  # noqa: BLE001 — une position non retenue ne doit pas gêner la fermeture
        print(f"{maintenant()} position de la fenêtre : {e}")


def _restaurer_placement():
    """Remet la fenêtre où elle était, avant son affichage. Si cet écran n'est plus là, elle s'ouvre comme la première fois."""
    global _placement_ecrit
    try:
        from ctypes import wintypes

        from System.Drawing import Size
        from System.Windows.Forms import FormStartPosition, FormWindowState
        placement = lire_json(FICHIER_FENETRE, None)
        if not placement:
            return                              # première ouverture : centrée sur l'écran principal
        gauche, haut, droite, bas = (int(v) for v in placement["rect"])
        user32 = ctypes.windll.user32
        user32.MonitorFromRect.restype = wintypes.HMONITOR
        rect = wintypes.RECT(gauche, haut, droite, bas)
        if droite <= gauche or bas <= haut or not user32.MonitorFromRect(ctypes.byref(rect), 0):   # 0 : aucun écran
            return
        form = FENETRE.native
        form.StartPosition = FormStartPosition.Manual
        # Le minimum vaut pour l'échelle de l'écran principal : il agrandirait la fenêtre sur un écran à plus
        # petite échelle. _suivre_echelle remet le bon juste après l'affichage.
        form.MinimumSize = Size(0, 0)
        p = PlacementFenetre(length=ctypes.sizeof(PlacementFenetre), showCmd=0)   # SW_HIDE : pas encore affichée
        p.rcNormalPosition[:] = [gauche, haut, droite, bas]
        user32.SetWindowPlacement(wintypes.HWND(int(form.Handle.ToInt64())), ctypes.byref(p))
        if placement.get("agrandie"):
            form.WindowState = FormWindowState.Maximized
        _placement_ecrit = placement
    except Exception as e:  # noqa: BLE001 — au pire elle s'ouvre centrée
        print(f"{maintenant()} position de la fenêtre : {e}")


def _suivre_placement():
    FENETRE.native.ResizeEnd += _enregistrer_placement   # fin d'un déplacement ou d'un redimensionnement
    FENETRE.events.maximized += _enregistrer_placement
    FENETRE.events.restored += _enregistrer_placement
    FENETRE.events.closing += _enregistrer_placement


def limiter_liens_externes():
    """Les liens viennent en partie des fichiers .toc des addons : seuls http et https sortent de la fenêtre.
    Sinon Windows lancerait le programme lié à n'importe quel protocole (calculator:, search-ms:…)."""
    ouvrir = webbrowser.open

    def filtre(url, *arguments, **options):
        if urllib.parse.urlparse(str(url)).scheme.lower() not in ("http", "https"):
            print(f"{maintenant()} lien refusé : {str(url)[:80]}")
            return False
        return ouvrir(url, *arguments, **options)

    webbrowser.open = filtre


def fenetre_seule():
    """Processus --fenetre : montre l'interface, et se termine quand on ferme la fenêtre."""
    global FENETRE
    limiter_liens_externes()
    import ctypes
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    import webview
    DOSSIER_WEBVIEW.mkdir(parents=True, exist_ok=True)
    FENETRE = webview.create_window(NOM, adresse_locale(), width=1280, height=880, min_size=TAILLE_MINI,
                                    background_color="#0f0d0a")
    FENETRE.events.before_show += _restaurer_placement
    FENETRE.events.before_show += _suivre_placement
    FENETRE.events.shown += _apres_affichage
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(DOSSIER_WEBVIEW))


_verrou_fenetre = threading.Lock()
_derniere_ouverture = 0.0


def ouvrir_edge():
    """Repli sans moteur intégré : ramène la fenêtre Edge si elle existe, sinon l'ouvre (mode application).
    Une seule fenêtre : seule l'instance principale ouvre, sous verrou, et si une ouverture est en cours
    (Edge met une seconde à afficher la fenêtre) on attend qu'elle apparaisse au lieu d'en lancer une autre."""
    global _derniere_ouverture
    import time
    with _verrou_fenetre:
        if ramener_fenetre():
            return
        if time.monotonic() - _derniere_ouverture < 10:
            for _ in range(40):                     # jusqu'à 8 s
                time.sleep(0.2)
                if ramener_fenetre():
                    return
        _derniere_ouverture = time.monotonic()
        edge = chercher_edge()
        if edge:
            subprocess.Popen([str(edge), f"--app={adresse_locale()}", "--window-size=1280,880"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            webbrowser.open(adresse_locale())


def demander_a_l_instance(lien=None):
    """l'appli tourne déjà : on lui demande d'ouvrir sa fenêtre (et de traiter un lien CurseForge), puis on s'arrête."""
    import ctypes
    ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
    req = urllib.request.Request(adresse_locale() + "api/fenetre", data=json.dumps({"lien": lien}).encode(),
                                 headers={"X-Hamax": "1", "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except OSError as e:
        print(f"{maintenant()} instance principale injoignable : {e}")


# --- Liens « Install » du site CurseForge --------------------------------------------

EVENEMENTS = []   # messages à afficher dans l'interface (installation lancée depuis le site…)


def traiter_lien(lien):
    """curseforge://install/?addonId=914482&fileId=…  →  installe la dernière version WoW Forever de l'addon.
    Le fileId est ignoré exprès : depuis une page « retail », il désignerait un fichier qui ne marche pas sur Forever."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(lien).query)
    projet = (q.get("addonId") or q.get("addonid") or [""])[0]
    if not projet.isdigit():
        EVENEMENTS.append({"type": "erreur", "texte": f"Lien CurseForge non reconnu : {lien[:80]}"})
        return
    try:
        r = ajouter_curseforge(projet)
        EVENEMENTS.append({"type": "ok", "texte": f"{r['nom']} installé depuis CurseForge, rangé dans « À tester »."})
    except Erreur as e:
        EVENEMENTS.append({"type": "erreur", "texte": f"Installation depuis CurseForge impossible : {e}"})
    except Exception as e:  # noqa: BLE001
        EVENEMENTS.append({"type": "erreur", "texte": f"Installation depuis CurseForge impossible : {type(e).__name__} : {e}"})


CLE_LIENS = r"Software\Classes\curseforge"


def commande_liens():
    cible, base = cible_lancement()
    return f'"{cible}" {base} "%1"'.replace("  ", " ")


def liens_curseforge_actifs():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_LIENS + r"\shell\open\command") as k:
            commande = str(winreg.QueryValueEx(k, "")[0]).lower()
            return NOM.lower() in commande or str(cible_lancement()[0]).lower() in commande
    except OSError:
        return False


def regler_liens_curseforge(actif):
    """Fait ouvrir les liens curseforge:// par l'appli (au lieu de l'appli CurseForge), ou rend la main.
    L'ancienne commande est gardée dans config.json pour pouvoir la remettre."""
    import winreg
    chemin_commande = CLE_LIENS + r"\shell\open\command"
    if actif:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, chemin_commande) as k:
                avant = winreg.QueryValueEx(k, "")[0]
            if "curseforge.exe" in str(avant).lower():
                modifier_config(curseforge_lien_avant=avant)
        except OSError:
            pass
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLE_LIENS) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:CurseForge")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, chemin_commande) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, commande_liens())
    else:
        avant = charger_config().get("curseforge_lien_avant")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, chemin_commande) as k:
            if avant:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, avant)
            else:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "")
    return {"liens_curseforge": liens_curseforge_actifs()}


ICONE = None          # icône de la zone de notification (pystray), si disponible
SERVEUR = None


def demander_arret():
    """Quitter : depuis le menu de l'icône ou le bouton de l'interface."""
    _arret_demande.set()
    if _processus_fenetre is not None and _processus_fenetre.poll() is None:
        _processus_fenetre.terminate()
    if ICONE is not None:
        ICONE.stop()
    return {}


def creer_icone():
    """Icône à côté de l'horloge, comme CurseForge. None si pystray n'est pas installé (mode script)."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None

    def verifier_maintenant():
        def tache():
            try:
                verifier_et_signaler()
            except Exception as e:  # noqa: BLE001
                notifier(NOM, f"Vérification impossible : {e}")
        threading.Thread(target=tache, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Ouvrir", ouvrir_interface, default=True),
        pystray.MenuItem("Vérifier les mises à jour", verifier_maintenant),
        pystray.MenuItem("Lancer au démarrage de Windows",
                         lambda: regler_demarrage(not demarrage_actif()),
                         checked=lambda item: demarrage_actif()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Version {VERSION}", None, enabled=False),
        pystray.MenuItem("Quitter", demander_arret),
    )
    class IconeNette(pystray.Icon):
        """pystray convertit l'image en .ico puis demande à Windows la taille « normale » (32 px),
        que Windows réduit pour l'horloge : flou. On charge plutôt notre .ico, dessiné à la main
        pour chaque taille, directement à la taille des petites icônes (16, 20 ou 24 px selon l'échelle)."""

        def _assert_icon_handle(self):
            if self._icon_handle:
                return
            try:
                import ctypes
                from pystray._util import win32
                taille = ctypes.windll.user32.GetSystemMetrics(49)   # SM_CXSMICON, à l'échelle de l'écran
                self._icon_handle = win32.LoadImage(None, str(DOSSIER_UI / "icone.ico"), win32.IMAGE_ICON,
                                                    taille, taille, win32.LR_LOADFROMFILE)
            except Exception:  # noqa: BLE001 — en cas de souci, le chargement d'origine de pystray
                self._icon_handle = None
            if not self._icon_handle:
                super()._assert_icon_handle()

    return IconeNette("ForeverAddonsManager", Image.open(DOSSIER_UI / "icone.png"), NOM, menu)


# --- Serveur HTTP ---------------------------------------------------------------

class Gestionnaire(BaseHTTPRequestHandler):
    server_version = "ForeverAddonsManager"

    def log_message(self, *args):
        pass

    def envoyer(self, code, donnees):
        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    @staticmethod
    def fenetre(lien):
        """Demande d'une deuxième instance : traiter le lien éventuel, puis ouvrir (ou ramener) la fenêtre."""
        def tache():
            if lien:
                traiter_lien(lien)
            ouvrir_interface()
        threading.Thread(target=tache, daemon=True).start()
        return {}

    def envoyer_fichier(self, fichier, type_contenu):
        corps = fichier.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", type_contenu)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        chemin = urllib.parse.urlparse(self.path).path
        if chemin == "/":
            self.envoyer_fichier(DOSSIER_UI / "index.html", "text/html; charset=utf-8")
        elif chemin == "/icone.svg":
            self.envoyer_fichier(DOSSIER_UI / "icone.svg", "image/svg+xml")
        elif chemin == "/icone-512.png":
            self.envoyer_fichier(DOSSIER_UI / "icone-512.png", "image/png")
        elif chemin == "/manifest.webmanifest":
            # Edge prend l'icône de la fenêtre (barre des tâches) dans le manifeste : en grand, donc nette
            corps = json.dumps({
                "name": NOM, "short_name": NOM, "start_url": "/", "display": "standalone",
                "background_color": "#0f0d0a", "theme_color": "#0f0d0a",
                "icons": [
                    {"src": "/icone-512.png?i=3", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                    {"src": "/icone.png?i=3", "sizes": "256x256", "type": "image/png", "purpose": "any"},
                    {"src": "/icone.svg?i=3", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                ],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
        elif chemin == "/icone.png":
            self.envoyer_fichier(DOSSIER_UI / "icone.png", "image/png")
        elif chemin == "/polices/BarlowCondensed.ttf":
            self.envoyer_fichier(DOSSIER_UI / "polices" / "BarlowCondensed.ttf", "font/ttf")
        elif chemin == "/api/etat":
            self.envoyer(200, etat())
        elif chemin == "/api/progression":
            self.envoyer(200, PROGRESSION)
        elif chemin == "/api/changelog":
            self.envoyer(200, {"version": VERSION, "texte": FICHIER_CHANGELOG.read_text(encoding="utf-8")})
        else:
            self.envoyer(404, {"erreur": "introuvable"})

    def do_POST(self):
        # Protège contre un site web qui viserait ce serveur local depuis le navigateur.
        port = self.server.server_address[1]
        if self.headers.get("X-Hamax") != "1" or self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self.envoyer(403, {"erreur": "refusé"})
        url = urllib.parse.urlparse(self.path)
        longueur = int(self.headers.get("Content-Length") or 0)
        corps = self.rfile.read(longueur) if longueur else b""
        try:
            if url.path == "/api/installer-zip":
                q = urllib.parse.parse_qs(url.query)
                return self.envoyer(200, installer_zip(corps, (q.get("id") or [None])[0], (q.get("nom") or [""])[0]))
            donnees = json.loads(corps or b"{}")
            routes = {
                "/api/addon": lambda: enregistrer_addon(donnees["addon"], donnees.get("ancien_id")),
                "/api/addon/supprimer": lambda: supprimer_du_registre(donnees["id"]) or {},
                "/api/verifier": lambda: verifier_tout(donnees.get("ids")),
                "/api/appli/maj": installer_maj_appli,
                "/api/appli/verifier": verifier_appli,
                "/api/installer": lambda: installer_distant(donnees["id"]),
                "/api/jeu/analyser": analyser_jeu,
                "/api/jeu/choisir": lambda: choisir_jeu(donnees["chemin"]),
                "/api/jeu/parcourir": parcourir_jeu,
                "/api/creation": ajouter_creation,
                "/api/liens-curseforge": lambda: regler_liens_curseforge(bool(donnees.get("actif"))),
                "/api/fenetre": lambda: self.fenetre(donnees.get("lien")),
                "/api/importer-curseforge": importer_curseforge,
                "/api/retirer": lambda: retirer(donnees["id"]),
                "/api/lier": lambda: lier(donnees["id"]),
                "/api/ouvrir": lambda: ouvrir(donnees["id"]),
                "/api/jouer": jouer,
                "/api/demarrage": lambda: regler_demarrage(bool(donnees.get("actif"))),
                "/api/quitter": demander_arret,
            }
            if url.path not in routes:
                return self.envoyer(404, {"erreur": "introuvable"})
            self.envoyer(200, routes[url.path]())
        except Erreur as e:
            self.envoyer(400, {"erreur": str(e)})
        except Exception as e:  # noqa: BLE001 — on remonte tout à l'interface
            self.envoyer(500, {"erreur": f"{type(e).__name__} : {e}"})


class Serveur(ThreadingHTTPServer):
    """Port réservé à une seule instance : sous Windows, SO_REUSEADDR (défaut de Python) laisse
    deux programmes écouter le même port, et une deuxième instance démarrerait au lieu d'ouvrir la fenêtre."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    global ICONE, SERVEUR
    migrer_donnees()
    RACINE.mkdir(parents=True, exist_ok=True)
    if "--importer-curseforge" in sys.argv:
        print(json.dumps(importer_curseforge(), ensure_ascii=False, indent=2))
        return
    if sys.stdout is None:  # .exe ou pythonw : pas de console, on écrit dans hamax.log (remis à zéro au-delà de 512 Ko)
        mode = "w" if FICHIER_JOURNAL.exists() and FICHIER_JOURNAL.stat().st_size > 512 * 1024 else "a"
        sys.stdout = sys.stderr = open(FICHIER_JOURNAL, mode, encoding="utf-8", buffering=1)
    if "--fenetre" in sys.argv:
        fenetre_seule()
        return
    if "--installation" in sys.argv:
        # Lancé par l'installateur. La première fois tout s'active ; ensuite on suit les choix de l'utilisateur.
        premiere = not charger_config().get("installation_faite")
        if premiere or demarrage_actif():
            regler_demarrage(True)
        if premiere or liens_curseforge_actifs():
            regler_liens_curseforge(True)
        modifier_config(installation_faite=True)
        return
    if "--desinstallation" in sys.argv:
        # Lancé par le désinstalleur : on rend tout à Windows. Les données restent, en cas de réinstallation.
        regler_demarrage(False)
        regler_liens_curseforge(False)
        try:
            urllib.request.urlopen(urllib.request.Request(adresse_locale() + "api/quitter", data=b"{}", method="POST", headers={"X-Hamax": "1", "Content-Type": "application/json"}), timeout=3)
            threading.Event().wait(6)
        except OSError:
            pass
        return
    # --demarrage : lancé par Windows à l'ouverture de session, sans ouvrir de fenêtre.
    silencieux = "--sans-navigateur" in sys.argv or "--demarrage" in sys.argv
    # Lien « Install » du site CurseForge (l'appli est le programme des liens curseforge://)
    lien = next((a for a in sys.argv[1:] if a.lower().startswith("curseforge://")), None)
    port = int(charger_config()["port"])
    try:
        SERVEUR = Serveur(("127.0.0.1", port), Gestionnaire)
    except OSError:
        # Déjà lancé (icône près de l'horloge) : c'est lui qui ouvre la fenêtre et traite le lien.
        if not silencieux or lien:
            demander_a_l_instance(lien)
        return
    if lien:
        threading.Thread(target=traiter_lien, args=(lien,), daemon=True).start()
        silencieux = False
    priorite_arriere_plan()
    try:  # conscient de l'échelle d'affichage : Windows n'agrandit plus (et ne floute plus) l'icône près de l'horloge
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass
    print(f"{maintenant()} {NOM} {VERSION} : {adresse_locale()}")
    shutil.rmtree(RACINE / "maj", ignore_errors=True)   # l'installateur de la dernière mise à jour
    arret = threading.Event()
    threading.Thread(target=verification_auto, args=(arret,), daemon=True).start()
    if not moteur_fenetre_disponible():   # fenêtre Edge : on ne peut que corriger sa taille après coup
        threading.Thread(target=surveiller_taille_fenetre, daemon=True).start()
    if not silencieux:
        threading.Thread(target=ouvrir_interface, daemon=True).start()
    ICONE = None if "--sans-icone" in sys.argv else creer_icone()
    threading.Thread(target=SERVEUR.serve_forever, kwargs={"poll_interval": 5}, daemon=True).start()
    try:
        if ICONE is not None:
            ICONE.run()                   # boucle de messages Windows, au repos
        else:
            _arret_demande.wait()
    except KeyboardInterrupt:
        pass
    finally:
        SERVEUR.shutdown()
        arret.set()
        print(f"{maintenant()} {NOM} arrêté")


if __name__ == "__main__":
    main()

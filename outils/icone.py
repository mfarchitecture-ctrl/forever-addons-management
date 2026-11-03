"""Icône de Hamax Addons Management : le « ! » de quête de WoW sur un badge rond.

Une seule géométrie (grille 64×64) :
  - ui/icone.svg : vectoriel, pour l'interface (barre de titre, barre des tâches, en-tête) ;
  - ui/icone.ico : pour l'exe et la zone de notification (Windows n'accepte pas le SVG à ces endroits).
    Chaque taille est dessinée à part, en grand puis réduite : nette jusqu'en 16 px.
  - ui/icone.png : 256 px, pour l'icône près de l'horloge.

    .venv\\Scripts\\python outils\\icone.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

RACINE = Path(__file__).resolve().parent.parent

# Badge : disque sombre éclairé en haut à gauche, anneau métallique
BADGE_CENTRE, BADGE_BORD = (58, 58, 58), (13, 13, 13)
ANNEAU = [(217, 217, 217), (110, 110, 110), (189, 189, 189)]
# « ! » jaune quête, cerclé de noir
JAUNE_HAUT, JAUNE_BAS = (255, 240, 122), (227, 164, 0)
CONTOUR = 3.2
# Deux dessins de la même icône, comme les icônes Windows soignées :
#  - « grand » (32 px et plus) : badge à fin anneau métallique, « ! » qui remplit le badge ;
#  - « petit » (24 px et moins) : pas d'anneau (il ne ferait qu'un pixel gris), disque plein bord à bord,
#    « ! » encore plus gros pour rester lisible dans la barre des tâches et près de l'horloge.
DESSINS = {
    "grand": {"anneau": (1.5, 62.5), "disque": (4.2, 59.8),
              "trait": [(25.5, 8.5), (38.5, 8.5), (35.3, 40.5), (28.7, 40.5)], "point": (32, 50.5, 6.6)},
    "petit": {"anneau": None, "disque": (0.5, 63.5),
              "trait": [(21.5, 3), (42.5, 3), (37.5, 40.5), (26.5, 40.5)], "point": (32, 52.5, 9.2)},
}

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <radialGradient id="fond" cx=".4" cy=".35" r=".75"><stop offset="0" stop-color="#3a3a3a"/><stop offset="1" stop-color="#0d0d0d"/></radialGradient>
    <linearGradient id="anneau" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d9d9d9"/><stop offset=".5" stop-color="#6e6e6e"/><stop offset="1" stop-color="#bdbdbd"/></linearGradient>
    <linearGradient id="jaune" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff07a"/><stop offset="1" stop-color="#e3a400"/></linearGradient>
  </defs>
  <circle cx="32" cy="32" r="30.5" fill="url(#anneau)"/>
  <circle cx="32" cy="32" r="27.8" fill="url(#fond)"/>
  <g stroke="#000" stroke-width="3.2" stroke-linejoin="round">
    <path d="M25.5 8.5h13l-3.2 32h-6.6Z" fill="url(#jaune)"/>
    <circle cx="32" cy="50.5" r="6.6" fill="url(#jaune)"/>
  </g>
</svg>
"""


def degrade_vertical(taille, couleurs):
    """Dégradé vertical passant par toutes les couleurs, réparties régulièrement."""
    img = Image.new("RGB", (1, taille))
    for y in range(taille):
        t = y / max(taille - 1, 1) * (len(couleurs) - 1)
        i = min(int(t), len(couleurs) - 2)
        f = t - i
        img.putpixel((0, y), tuple(round(couleurs[i][c] + (couleurs[i + 1][c] - couleurs[i][c]) * f) for c in range(3)))
    return img.resize((taille, taille)).convert("RGBA")


def degrade_radial(taille, centre, bord, cx=0.4, cy=0.35, r=0.75):
    petit = 64
    img = Image.new("RGB", (petit, petit))
    for y in range(petit):
        for x in range(petit):
            t = min(1.0, ((x / petit - cx) ** 2 + (y / petit - cy) ** 2) ** 0.5 / r)
            img.putpixel((x, y), tuple(round(centre[c] + (bord[c] - centre[c]) * t) for c in range(3)))
    return img.resize((taille, taille), Image.BICUBIC).convert("RGBA")


def rendre(taille):
    """Dessine l'icône à « taille » px : tracé à 6×, puis réduction lissée."""
    grand = taille * 6
    k = grand / 64
    img = Image.new("RGBA", (grand, grand), (0, 0, 0, 0))

    def masque(dessin):
        m = Image.new("L", (grand, grand), 0)
        dessin(ImageDraw.Draw(m))
        return m

    dessin = DESSINS["petit" if taille <= 24 else "grand"]
    if dessin["anneau"]:
        a0, a1 = (v * k for v in dessin["anneau"])
        img.paste(degrade_vertical(grand, ANNEAU), (0, 0), masque(lambda d: d.ellipse((a0, a0, a1, a1), fill=255)))
    d0, d1 = (v * k for v in dessin["disque"])
    img.paste(degrade_radial(grand, BADGE_CENTRE, BADGE_BORD), (0, 0), masque(lambda d: d.ellipse((d0, d0, d1, d1), fill=255)))

    trait = [(x * k, y * k) for x, y in dessin["trait"]]
    cx, cy, r = (v * k for v in dessin["point"])
    d = ImageDraw.Draw(img)
    # Contour noir : la forme pleine + un trait arrondi tout autour (comme stroke-linejoin="round")
    epaisseur = max(1, round(CONTOUR * k))
    d.polygon(trait, fill="black")
    d.line(trait + trait[:1], fill="black", width=epaisseur, joint="curve")
    for x, y in trait:
        d.ellipse((x - epaisseur / 2, y - epaisseur / 2, x + epaisseur / 2, y + epaisseur / 2), fill="black")
    d.ellipse((cx - r - epaisseur / 2, cy - r - epaisseur / 2, cx + r + epaisseur / 2, cy + r + epaisseur / 2), fill="black")

    def forme(dd):
        dd.polygon(trait, fill=255)
        dd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    img.paste(degrade_vertical(grand, [JAUNE_HAUT, JAUNE_BAS]), (0, 0), masque(forme))
    return img.resize((taille, taille), Image.LANCZOS)


def main():
    ui = RACINE / "ui"
    (ui / "icone.svg").write_text(SVG, encoding="utf-8")
    tailles = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = [rendre(t) for t in tailles]
    images[-1].save(ui / "icone.ico", sizes=[(t, t) for t in tailles], append_images=images[:-1])
    images[-1].save(ui / "icone.png")
    rendre(512).save(ui / "icone-512.png")   # pour le manifeste : grande icône de la fenêtre dans la barre des tâches
    print("ui/icone.svg, ui/icone.ico, ui/icone.png et ui/icone-512.png créés")


if __name__ == "__main__":
    main()

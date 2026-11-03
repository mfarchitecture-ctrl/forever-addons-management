"""Génère les images de l'installateur (installateur.iss) à partir de l'icône, en taille normale et pour les écrans à 200 %."""
from pathlib import Path

from PIL import Image

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "outils" / "installateur"
FOND = (18, 15, 11)


def image(largeur, hauteur, cote, nom):
    fond = Image.new("RGB", (largeur, hauteur), FOND)
    icone = Image.open(RACINE / "ui" / "icone-512.png").convert("RGBA").resize((cote, cote), Image.LANCZOS)
    fond.paste(icone, ((largeur - cote) // 2, (hauteur - cote) // 2), icone)
    fond.save(SORTIE / nom)


SORTIE.mkdir(exist_ok=True)
for echelle, suffixe in ((1, "100"), (2, "200")):
    image(164 * echelle, 314 * echelle, 104 * echelle, f"grande-{suffixe}.bmp")
    image(58 * echelle, 58 * echelle, 46 * echelle, f"petite-{suffixe}.bmp")

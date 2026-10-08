"""Seed an account's closet with ~25 placeholder items. Idempotent (matches on name).

make seed                        # the development account (DEV_USER_EMAIL)
make seed EMAIL=you@example.com  # an account you registered (or made with `make user`)
"""

import argparse
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw
from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.engine import vocab
from app.models import Category, Item, Source
from app.schemas import ItemData
from app.services.auth import find_user
from app.services.imaging import process_image
from app.services.items import create_item
from app.services.passwords import normalize_email
from app.services.storage import LocalDiskStorage
from app.services.users import get_or_create_user

ALL = ["spring", "summer", "autumn", "winter"]
WARM = ["spring", "summer"]
COOL = ["autumn", "winter"]
MID = ["spring", "autumn"]


@dataclass(frozen=True)
class Seed:
    name: str
    category: Category
    subtype: str
    color: str
    seasons: list[str]
    pattern: str = "solid"
    material: str | None = None
    layer: str | None = None
    secondary: tuple[str, ...] = ()


T, B, D, OUT, S, A = (
    Category.TOP,
    Category.BOTTOM,
    Category.DRESS,
    Category.OUTERWEAR,
    Category.SHOES,
    Category.ACCESSORY,
)

SEEDS: list[Seed] = [
    # tops (8)
    Seed("White tee", T, "t-shirt", "white", ALL, material="cotton", layer="base"),
    Seed("Black tee", T, "t-shirt", "black", ALL, material="cotton", layer="base"),
    Seed(
        "Breton stripe tee",
        T,
        "t-shirt",
        "navy",
        WARM + ["autumn"],
        "striped",
        "cotton",
        "base",
        ("white",),
    ),
    Seed("Light blue Oxford shirt", T, "shirt", "light blue", ALL, material="cotton", layer="base"),
    Seed("White linen shirt", T, "shirt", "white", WARM, material="linen", layer="base"),
    Seed("Navy polo", T, "polo", "navy", WARM, material="cotton", layer="base"),
    Seed("Grey crewneck sweater", T, "sweater", "grey", COOL, material="wool", layer="mid"),
    Seed(
        "Cream silk blouse",
        T,
        "blouse",
        "cream",
        MID + ["summer"],
        material="silk",
        layer="base",
    ),
    # bottoms (5)
    Seed("Blue straight jeans", B, "jeans", "blue", ALL, material="denim"),
    Seed("Black skinny jeans", B, "jeans", "black", ALL, material="denim"),
    Seed("Beige chinos", B, "chinos", "beige", MID + ["summer"], material="cotton"),
    Seed("Charcoal wool trousers", B, "trousers", "charcoal", COOL + ["spring"], material="wool"),
    Seed("Denim shorts", B, "shorts", "light blue", ["summer"], material="denim"),
    # dresses (2)
    Seed("Floral summer dress", D, "casual dress", "pink", WARM, "floral", "cotton"),
    Seed("Black cocktail dress", D, "cocktail dress", "black", ALL, material="polyester"),
    # outerwear (4)
    Seed("Navy blazer", OUT, "blazer", "navy", MID + ["winter"], material="wool", layer="outer"),
    Seed("Olive field jacket", OUT, "jacket", "olive", MID, material="cotton", layer="outer"),
    Seed("Camel wool coat", OUT, "coat", "tan", ["winter"], material="wool", layer="outer"),
    Seed("Burgundy cardigan", OUT, "cardigan", "burgundy", COOL, material="knit", layer="mid"),
    # shoes (4)
    Seed("White sneakers", S, "sneakers", "white", ALL, material="leather"),
    Seed("Black Chelsea boots", S, "boots", "black", COOL + ["spring"], material="leather"),
    Seed("Brown leather loafers", S, "loafers", "brown", ALL, material="leather"),
    Seed("Tan leather sandals", S, "sandals", "tan", ["summer"], material="leather"),
    # accessories (2)
    Seed("Brown leather belt", A, "belt", "brown", ALL, material="leather"),
    Seed("Grey wool scarf", A, "scarf", "grey", ["winter", "autumn"], material="wool"),
]


def placeholder_image(seed: Seed) -> bytes:
    """A 3:4 block of the item's color (with stripes when striped). No downloads."""
    base = vocab.COLOR_HEX[seed.color]
    image = Image.new("RGB", (600, 800), base)
    if seed.pattern == "striped" and seed.secondary:
        draw = ImageDraw.Draw(image)
        stripe = vocab.COLOR_HEX[seed.secondary[0]]
        for y in range(0, 800, 80):
            draw.rectangle((0, y + 40, 600, y + 79), fill=stripe)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class NoSuchAccount(Exception):
    pass


def seed_closet(
    session: Session, storage: LocalDiskStorage, email: str, create_dev_user: bool = False
) -> tuple[int, int]:
    """Returns (created, skipped). The account must exist; only the development account
    (`create_dev_user=True`) is created on demand, and it has no password."""
    user = get_or_create_user(session, email) if create_dev_user else find_user(session, email)
    if user is None:
        raise NoSuchAccount(email)
    owned = select(Item.name).where(Item.user_id == user.id, Item.source == Source.OWNED)
    existing = set(session.exec(owned).all())
    created = skipped = 0
    for seed in SEEDS:
        if seed.name in existing:
            skipped += 1
            continue
        data = ItemData(
            category=seed.category,
            subtype=seed.subtype,
            primary_color=seed.color,
            secondary_colors=list(seed.secondary),
            pattern=seed.pattern,
            seasons=seed.seasons,
            material=seed.material,
            layer_role=seed.layer,
            name=seed.name,
        )
        raw = placeholder_image(seed)
        image = process_image(raw, max_bytes=len(raw) + 1)
        create_item(session, storage, user, data, image)
        created += 1
    return created, skipped


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default=settings.dev_user_email, help="the account to seed")
    args = parser.parse_args()
    email = normalize_email(args.email)
    try:
        with Session(get_engine()) as session:
            created, skipped = seed_closet(
                session,
                LocalDiskStorage(settings.media_root),
                email,
                create_dev_user=email == settings.dev_user_email,
            )
    except NoSuchAccount:
        raise SystemExit(
            f"No account for {email}. Register at /register, or run: make user EMAIL={email}"
        ) from None
    print(f"Seeded {email}: {created} created, {skipped} already present.")


if __name__ == "__main__":
    main()

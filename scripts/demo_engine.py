"""Run the outfit engine on the dev user's closet and print what it finds.

make seed && make demo
"""

import argparse

from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine
from app.engine.config import EngineConfig
from app.engine.generator import closet_stats, count_outfits, generate_outfits
from app.engine.types import Context
from app.services.auth import find_user
from app.services.outfits import load_closet
from app.services.passwords import normalize_email


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", help="spring, summer, autumn or winter")
    parser.add_argument("--email", help="the account to use (default: DEV_USER_EMAIL)")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default: 1)")
    args = parser.parse_args()

    config = EngineConfig()
    context = Context(season=args.season)
    with Session(get_engine()) as session:
        email = normalize_email(args.email or get_settings().dev_user_email)
        user = find_user(session, email)
        if user is None:
            raise SystemExit(f"No account for {email}. Register first, or run: make user")
        closet = load_closet(session, user)

    def label(item_id: int) -> str:
        item = closet.items[item_id]
        return item.name or item.subtype or f"item {item_id}"

    chosen = {"season": args.season}
    where = ", ".join(f"{k}={v}" for k, v in chosen.items() if v)
    suffix = f" ({where})" if where else ""
    print(f"{len(closet.views)} confirmed items for {user.email}{suffix}")

    print("\n== 5 outfits ==")
    for n, outfit in enumerate(generate_outfits(closet.views, context, config, 5, args.seed), 1):
        print(f"\n{n}. score {outfit.score:.2f}: " + " + ".join(label(i) for i in outfit.item_ids))
        for reason in outfit.reasons:
            print(f"     - {reason}")

    result = count_outfits(closet.views, config, context=context)
    print(f"\n== Valid outfits: {result.count} ==")

    stats = closet_stats(closet.views, config, context)
    ranked = sorted(stats.per_item.items(), key=lambda pair: (-pair[1], pair[0]))
    print("\n== 5 most versatile pieces ==")
    for item_id, count in ranked[:5]:
        print(f"  {count:>4}  {label(item_id)}")
    print("\n== 5 least versatile pieces ==")
    for item_id, count in reversed(ranked[-5:]):
        print(f"  {count:>4}  {label(item_id)}")


if __name__ == "__main__":
    main()

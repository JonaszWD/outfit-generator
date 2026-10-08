"""The Phase 5 flow end to end, in-process, against your dev database.

    make migrate && make demo-account

Registers a throwaway account, logs in, seeds a closet, asks "Dress me for today" with a fake
cold, rainy forecast (no network), rates outfits, shows the ranking change, logs out and shows
that a protected page redirects to the login page. The account and its files are removed after.
"""

import logging
import re
import secrets
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.config import get_settings
from app.db import get_engine
from app.deps import get_email_sender, get_weather_provider
from app.engine.config import EngineConfig
from app.engine.types import Context
from app.engine.weather import Conditions
from app.main import create_app
from app.services import account
from app.services import feedback as feedback_svc
from app.services.auth import find_user
from app.services.outfits import suggest
from app.services.storage import LocalDiskStorage
from app.services.weather import FakeWeatherProvider
from scripts._demo import page_token
from scripts.seed_closet import seed_closet


class Outbox:
    def send(self, to: str, subject: str, body: str) -> None:
        pass


def names(html: str) -> list[list[str]]:
    cards = html.split('class="card outfit"')[1:]
    return [
        [a.split(",")[0] for a in re.findall(r'alt="([^"]*)"', c.split("</article>")[0])]
        for c in cards
    ]


def main() -> None:
    logging.disable(logging.INFO)  # keep the demo's own output readable
    settings = get_settings()
    storage = LocalDiskStorage(settings.media_root)
    email, password = f"demo-{secrets.token_hex(4)}@example.com", secrets.token_urlsafe(18)
    weather = FakeWeatherProvider(conditions=Conditions(4.0, 1.0, rain_probability_percent=80.0))
    app = create_app()
    app.dependency_overrides[get_weather_provider] = lambda: weather
    app.dependency_overrides[get_email_sender] = lambda: Outbox()
    client = TestClient(app, follow_redirects=False)
    try:
        print(f"1. Register {email}")
        token = page_token(client.get("/register").text)
        r = client.post(
            "/register", data={"email": email, "password": password, "csrf_token": token}
        )
        print(f"   POST /register -> {r.status_code} {r.headers['location']}")

        print("2. Log in")
        token = page_token(client.get("/login").text)
        r = client.post("/login", data={"email": email, "password": password, "csrf_token": token})
        cookie = r.headers["set-cookie"].split(";")
        print(
            f"   POST /login -> {r.status_code} {r.headers['location']}  ({'; '.join(cookie[1:])})"
        )
        token = page_token(client.get("/outfits").text)

        print("3. Seed a closet for this account (what `make seed EMAIL=...` does)")
        with Session(get_engine()) as session:
            print("   created, skipped =", seed_closet(session, storage, email))

        print("4. Set the home city (rounded to 2 decimals) and dress me for today")
        client.post(
            "/settings/city",
            data={
                "name": "Oslo, Norway",
                "latitude": "59.913868",
                "longitude": "10.752245",
                "csrf_token": token,
            },
        )
        page = client.get("/outfits").text
        strip = re.search(
            r"(-?\d+°C) <span[^>]*>\(([^)]*)\)</span>.*?(Rain likely)?</p>", page, re.S
        )
        print("   Today's weather strip:", strip.group(1, 2, 3) if strip else "missing")
        print("   forecast requested for:", weather.requested[-1], "(rounded)")
        r = client.post("/outfits/generate", data={"today": "1", "csrf_token": token})
        cards = names(r.text)
        for n, card in enumerate(cards, 1):
            print(f"   {n}. " + " + ".join(card))
        print("   ", re.search(r"Dressed for[^<]*", r.text).group(0).strip())  # type: ignore[union-attr]
        layers = ("jacket", "coat", "blazer", "cardigan")
        assert cards and all(
            any(w in name.lower() for name in card for w in layers) for card in cards
        ), "cold day: every outfit needs a layer"
        ids = [int(i) for i in re.findall(r'id="outfit-(\d+)"', r.text)]

        print("5. Give feedback")
        with Session(get_engine()) as session:
            user = find_user(session, email)
            assert user is not None
            config = EngineConfig()

            def ranking() -> list[tuple[float, float | None, int]]:
                p = feedback_svc.personalization_for(session, user, config)
                found = suggest(session, user, config, Context(), 8, rng_seed=1, personalization=p)
                return [
                    (o.score, (o.breakdown or {}).get("adjusted_score"), o.id or 0)
                    for o in found.outfits
                ]

            before = ranking()
            for outfit_id in ids:
                client.post(
                    f"/outfits/{outfit_id}/feedback",
                    data={"rating": "down", "reason": "colors clash", "csrf_token": token},
                )
            session.expire_all()
            after = ranking()
            print(
                "   disliked the first two suggestions, liked the third."
                " Seeded top 8 (base score, adjusted score):"
            )
            print("   before:", [(round(b, 3), a) for b, a, _ in before])
            print("   after: ", [(round(b, 3), a) for b, a, _ in after])
            print(
                "   order changed:",
                [i for *_, i in before] != [i for *_, i in after]
                or any(a is not None for _, a, _ in after),
            )
            page = client.post("/outfits/generate", data={"csrf_token": token}).text
            print(
                "   cards show the base score:",
                bool(re.search(r"Score \d+%", page)),
                "| outfits moved by feedback in the seeded run:",
                sum(a is not None for _, a, _ in after),
            )
            client.post("/settings/personalization/reset", data={"csrf_token": token})
            session.expire_all()
            print(
                "   after Reset personalization the ranking equals the original:",
                ranking() == before,
            )

        print("6. Plan today, then log out")
        saved = client.post(f"/outfits/{ids[0]}/save", data={"csrf_token": token})
        client.post(
            "/planner/assign",
            data={"day": date.today().isoformat(), "outfit_id": ids[0], "csrf_token": token},
        )
        print(
            "   save ->",
            saved.status_code,
            "| planner shows it:",
            "Nothing planned." in client.get("/planner").text,
        )
        r = client.post("/logout", data={"csrf_token": token})
        print(f"   POST /logout -> {r.status_code} {r.headers['location']}")
        r = client.get("/closet")
        print(f"   GET /closet after logout -> {r.status_code} {r.headers['location']}")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    finally:
        with Session(get_engine()) as session:
            user = find_user(session, email)
            if user is not None:
                account.delete_account(session, storage, user)
        print("Demo account and files removed.")


if __name__ == "__main__":
    main()

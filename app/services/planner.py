"""The weekly planner: saved outfits assigned to days, with warnings.

Warnings never block a plan. They say when the same piece is worn on consecutive days, when the
forecast does not suit the outfit, and when a piece is in the laundry, unavailable or archived.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, delete, select

from app.engine.config import EngineConfig
from app.engine.rules import check_seasons, check_weather
from app.engine.types import Context
from app.engine.weather import apply_weather
from app.models import Availability, Item, Outfit, PlannedOutfit, Source, User
from app.services.outfits import to_view
from app.services.weather import Forecast, format_temperature


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_days(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]


def parse_day(raw: str | None, default: date | None = None) -> date | None:
    try:
        return date.fromisoformat(raw or "")
    except ValueError:
        return default


@dataclass
class PlannedDay:
    day: date
    planned: PlannedOutfit | None = None
    outfit: Outfit | None = None
    items: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def assign(session: Session, user: User, day: date, outfit: Outfit) -> None:
    """One outfit per day; assigning again replaces it."""
    values = {"outfit_id": outfit.id}
    session.execute(
        insert(PlannedOutfit)
        .values(user_id=user.id, day=day, **values)
        .on_conflict_do_update(constraint="uq_planned_user_day", set_=values)
    )
    session.commit()


def clear(session: Session, user: User, day: date) -> None:
    session.exec(
        delete(PlannedOutfit).where(
            col(PlannedOutfit.user_id) == user.id, col(PlannedOutfit.day) == day
        )
    )
    session.commit()


def _name(item: Item) -> str:
    return item.name or item.subtype or "piece"


def _joined(names: list[str]) -> str:
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]


def _weather_warning(
    items: list[Item], day: date, forecast: Forecast, config: EngineConfig, units: str
) -> str | None:
    conditions = forecast.for_day(day)
    views = [v for i in items if (v := to_view(i)) is not None]
    if conditions is None or len(views) != len(items):
        return None
    context = apply_weather(Context(), conditions, config.weather)
    failure = next(
        (
            r
            for r in (check_seasons(views, context), check_weather(views, context, config))
            if not r.passed
        ),
        None,
    )
    if failure is None:
        return None
    forecast_text = format_temperature(conditions.representative_c, units)
    if context.rain:
        forecast_text += ", rain likely"
    return f"{failure.reason} (forecast {forecast_text})."


def plan_week(
    session: Session,
    user: User,
    start: date,
    forecast: Forecast,
    config: EngineConfig,
    units: str = "metric",
) -> list[PlannedDay]:
    days = week_days(start)
    first, last = days[0] - timedelta(days=1), days[-1]
    rows = list(
        session.exec(
            select(PlannedOutfit).where(
                PlannedOutfit.user_id == user.id,
                col(PlannedOutfit.day) >= first,
                col(PlannedOutfit.day) <= last,
            )
        )
    )
    by_day = {r.day: r for r in rows}
    outfits = {
        o.id: o
        for o in session.exec(
            select(Outfit).where(
                Outfit.user_id == user.id, col(Outfit.id).in_([r.outfit_id for r in rows])
            )
        )
    }
    wanted = {i for o in outfits.values() for i in o.item_ids}
    items = {
        i.id: i
        for i in session.exec(
            select(Item).where(
                Item.user_id == user.id,
                col(Item.source) == Source.OWNED,
                col(Item.id).in_(wanted),
            )
        )
    }

    def pieces(day: date) -> list[Item]:
        row = by_day.get(day)
        outfit = outfits.get(row.outfit_id) if row else None
        return [items[i] for i in outfit.item_ids if i in items] if outfit else []

    plan: list[PlannedDay] = []
    for day in days:
        row = by_day.get(day)
        entry = PlannedDay(day, row, outfits.get(row.outfit_id) if row else None, pieces(day))
        if entry.planned and entry.outfit:
            before = {i.id for i in pieces(day - timedelta(days=1))}
            repeated = [_name(i) for i in entry.items if i.id in before]
            if repeated:
                entry.warnings.append(f"{_joined(repeated)} also worn the day before.")
            if message := _weather_warning(entry.items, day, forecast, config, units):
                entry.warnings.append(message)
            away = [_name(i) for i in entry.items if i.availability != Availability.AVAILABLE]
            if away:
                entry.warnings.append(f"In the laundry or unavailable: {_joined(away)}.")
            archived = [_name(i) for i in entry.items if i.archived_at is not None]
            if archived:
                entry.warnings.append(f"Archived: {_joined(archived)}.")
            if len(entry.items) < len(entry.outfit.item_ids):
                entry.warnings.append("A piece of this outfit no longer exists.")
        plan.append(entry)
    return plan

from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.engine.config import EngineConfig
from app.models import Candidate, Item, User
from app.services.auth import SESSION_COOKIE, user_for_token
from app.services.candidates import CandidateDeps, get_owned_candidate
from app.services.items import get_owned_item
from app.services.mailer import EmailSender, build_email_sender
from app.services.pipeline import PipelineDeps
from app.services.preferences import engine_config_for
from app.services.storage import Storage, build_storage
from app.services.style import StyleExtractor, build_style_extractor
from app.services.weather import WeatherProvider, build_weather_provider


class NotAuthenticated(Exception):
    """No valid session. The app turns this into a redirect to the login page (or a 401)."""


def get_optional_user(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    user = user_for_token(session, token) if token else None
    if user is not None:
        request.state.user_id = user.id
    return user


def get_current_user(
    user: Annotated[User | None, Depends(get_optional_user)],
) -> User:
    """The logged-in user. Every route except the public allowlist depends on this, so
    swapping the authentication scheme means changing only this function."""
    if user is None:
        raise NotAuthenticated
    return user


def get_email_sender(settings: Annotated[Settings, Depends(get_settings)]) -> EmailSender:
    return build_email_sender(settings)


def get_storage(settings: Annotated[Settings, Depends(get_settings)]) -> Storage:
    return build_storage(settings)


def get_pipeline_deps() -> PipelineDeps | None:
    """None means "build the defaults from settings inside process_item". Tests override this."""
    return None


def get_style_extractor(settings: Annotated[Settings, Depends(get_settings)]) -> StyleExtractor:
    """Reads the style of a reference photo. Tests override this with the offline fake."""
    return build_style_extractor(settings)


def get_candidate_deps() -> CandidateDeps | None:
    """None means "build the defaults from settings inside analyze_candidate". Tests override."""
    return None


def owned_candidate(
    candidate_id: int,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Candidate:
    return get_owned_candidate(session, user, candidate_id)


def get_engine_config(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EngineConfig:
    """Outfit-engine thresholds and weights with the user's own preferences applied."""
    return engine_config_for(session, user)


_providers: dict[tuple[str, int, float], WeatherProvider] = {}


def _provider_for(settings: Settings) -> WeatherProvider:
    key = (
        settings.weather_provider,
        settings.weather_cache_minutes,
        settings.weather_timeout_seconds,
    )
    if key not in _providers:
        _providers[key] = build_weather_provider(settings)
    return _providers[key]


def get_weather_provider(settings: Annotated[Settings, Depends(get_settings)]) -> WeatherProvider:
    """One provider per process, so its forecast cache survives between requests."""
    return _provider_for(settings)


def owned_item(
    item_id: int,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> Item:
    return get_owned_item(session, user, item_id)


WeatherDep = Annotated[WeatherProvider, Depends(get_weather_provider)]
EngineConfigDep = Annotated[EngineConfig, Depends(get_engine_config)]
PipelineDepsDep = Annotated[PipelineDeps | None, Depends(get_pipeline_deps)]
CandidateDepsDep = Annotated[CandidateDeps | None, Depends(get_candidate_deps)]
OwnedCandidate = Annotated[Candidate, Depends(owned_candidate)]
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]
DbSession = Annotated[Session, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
StorageDep = Annotated[Storage, Depends(get_storage)]
StyleExtractorDep = Annotated[StyleExtractor, Depends(get_style_extractor)]
OwnedItem = Annotated[Item, Depends(owned_item)]

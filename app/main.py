from fastapi import FastAPI

from app.routes import health, pages


def create_app() -> FastAPI:
    app = FastAPI(title="Wardrobe")
    app.include_router(health.router)
    app.include_router(pages.router)
    return app

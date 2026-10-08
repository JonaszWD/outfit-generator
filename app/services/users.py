from sqlmodel import Session, select

from app.models import User


def get_or_create_user(session: Session, email: str) -> User:
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user

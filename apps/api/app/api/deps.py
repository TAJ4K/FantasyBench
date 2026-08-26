from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.db.session import get_db

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def require_admin(
    settings: AppSettings,
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if not x_admin_api_key or not secrets.compare_digest(x_admin_api_key, settings.admin_api_key):
        raise DomainError(
            "UNAUTHORIZED",
            "Invalid admin API key.",
            status_code=401,
        )


AdminAccess = Annotated[None, Depends(require_admin)]

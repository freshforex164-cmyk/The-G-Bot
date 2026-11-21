"""Validated broker-only browser cookies; never persist Google cookies or passwords."""
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

BROKER_HOSTS = ("pocketoption.com", "po.market", "po.cash")


def broker_domain(domain: str) -> bool:
    host = domain.lower().lstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in BROKER_HOSTS)


class BrokerCookie(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=8192)
    domain: str = Field(min_length=1, max_length=255)
    path: str = Field(default="/", min_length=1, max_length=2048, pattern=r"^/")
    expires: float = Field(default=-1, allow_inf_nan=False)
    httpOnly: bool = False
    secure: bool = True
    sameSite: Literal["Strict", "Lax", "None"] = "Lax"


def sanitize_browser_session(state: dict) -> dict:
    """Allow only the broker-cookie schema. Validation errors never include cookie values."""
    try:
        if set(state) != {"cookies"} or not isinstance(state["cookies"], list) or len(state["cookies"]) > 100:
            raise ValueError
        cookies = []
        for raw in state["cookies"]:
            if not isinstance(raw, dict):
                raise ValueError
            if not broker_domain(str(raw.get("domain", ""))):
                continue
            cookies.append(BrokerCookie.model_validate(raw).model_dump())
        result = {"cookies": cookies}
        if len(json.dumps(result).encode()) > 131072:
            raise ValueError
        return result
    except (ValidationError, ValueError, TypeError, KeyError):
        raise ValueError("Invalid broker browser session.") from None
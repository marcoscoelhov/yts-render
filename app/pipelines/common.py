from __future__ import annotations

from typing import Any


class RecoverableStepError(RuntimeError):
    pass


class FatalStepError(RuntimeError):
    pass


def model_payload(model: type[Any], payload: dict[str, Any]) -> dict[str, Any]:
    columns = {column.key for column in model.__mapper__.columns}
    return {key: value for key, value in payload.items() if key in columns}

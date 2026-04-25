from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, JSON, create_engine

from app.models import (
    ErrorLog,
    FallbackEvent,
    Job,
    NarrationAsset,
    RenderOutput,
    ReviewRecord,
    SceneAsset,
    ScenePlan,
    Script,
    StepExecution,
    SubtitleTrack,
    TopicPlan,
    TopicRegistry,
    TopicRequest,
)


TABLES = [
    Job,
    TopicRequest,
    TopicPlan,
    Script,
    ScenePlan,
    SceneAsset,
    NarrationAsset,
    SubtitleTrack,
    RenderOutput,
    ReviewRecord,
    FallbackEvent,
    ErrorLog,
    StepExecution,
    TopicRegistry,
]


def convert_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, JSON):
        if isinstance(value, str):
            return json.loads(value)
        return value
    if isinstance(column.type, DateTime):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value
    return value


def migrate(sqlite_url: str, postgres_url: str) -> None:
    sqlite_path = sqlite_url.removeprefix("sqlite:///")
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row

    target_engine = create_engine(postgres_url, future=True)
    with target_engine.begin() as conn:
        for model in reversed(TABLES):
            conn.execute(model.__table__.delete())
        for model in TABLES:
            table = model.__table__
            rows = source.execute(f"SELECT * FROM {table.name}").fetchall()
            if not rows:
                print(table.name, 0)
                continue
            payload = []
            for row in rows:
                item = {}
                for column in table.columns:
                    item[column.name] = convert_value(column, row[column.name])
                payload.append(item)
            conn.execute(table.insert(), payload)
            print(table.name, len(payload))


if __name__ == "__main__":
    migrate(
        sqlite_url="sqlite:///data/yts_render.db",
        postgres_url="postgresql+psycopg://yts_render:yts_render@127.0.0.1:5432/yts_render",
    )

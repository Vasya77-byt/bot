"""Standalone FastAPI-сервер с моком SBIS.

Запускается отдельно (например, через docker-compose) и обслуживает
тестовые HTTP-запросы. Сама фикстура mock_company лежит в
sbis_fixtures.py — оттуда же её импортирует SbisClient в режиме
SBIS_MOCK=true, не таща FastAPI в прод.
"""
import os
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

from sbis_fixtures import mock_company


app = FastAPI(title="SBIS Mock")


@app.post("/service/")
async def get_org_info(payload: Dict[str, Any]) -> JSONResponse:
    inn = payload.get("inn") or "0000000000"
    data = mock_company(inn).model_dump()
    return JSONResponse(content=data)


if __name__ == "__main__":
    port = int(os.getenv("SBIS_MOCK_PORT", "8081"))
    uvicorn.run("sbis_mock:app", host="0.0.0.0", port=port, reload=False)

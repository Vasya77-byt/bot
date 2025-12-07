import os
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn


app = FastAPI(title="SBIS Mock")


@app.post("/service/")
async def get_org_info(payload: Dict[str, Any]) -> JSONResponse:
    inn = payload.get("inn") or "0000000000"
    data = {
        "inn": inn,
        "name": f"Тестовая компания {inn}",
        "ogrn": "1234567890123",
        "region": "Москва",
        "reg_date": "2020-01-01",
        "age_years": 4,
        "okved_main": "62.01",
        "employees_count": 25,
        "revenue_last_year": 18000000,
        "profit_last_year": 2500000,
        "licenses": ["нет лицензий"],
    }
    return JSONResponse(content=data)


if __name__ == "__main__":
    port = int(os.getenv("SBIS_MOCK_PORT", "8081"))
    uvicorn.run("sbis_mock:app", host="0.0.0.0", port=port, reload=False)
from typing import Optional

from schemas import CompanyData


def mock_company(inn: Optional[str]) -> CompanyData:
    return CompanyData(
        inn=inn or "0000000000",
        name="ООО «Мокап»",
        ogrn="0000000000000",
        region="Москва",
        reg_date="2019-01-01",
        age_years=5,
        okved_main="62.01 Разработка ПО",
        employees_count=25,
        revenue_last_year=120_000_000,
        profit_last_year=18_000_000,
        licenses=["нет лицензий"],
    )


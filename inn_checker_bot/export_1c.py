"""
Генерация XML-файла с реквизитами контрагента для импорта в 1С.
Формат: КонтрагентыКоммерческаяИнформация (XML).
"""

from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring


def generate_1c_xml(fields: dict[str, Any]) -> bytes:
    """
    Генерирует XML-файл с реквизитами контрагента для 1С.

    Возвращает bytes с XML-контентом.
    """
    root = Element("КоммерческаяИнформация")
    root.set("ВерсияСхемы", "2.10")
    root.set("ДатаФормирования", _now_str())

    contragent = SubElement(root, "Контрагент")

    entity_type = fields.get("entity_type", "ul")

    # ИНН
    inn = fields.get("inn", "")
    SubElement(contragent, "ИНН").text = inn

    # КПП (только для ЮЛ)
    if entity_type != "ip" and fields.get("kpp"):
        SubElement(contragent, "КПП").text = fields["kpp"]

    # ОГРН
    ogrn = fields.get("ogrn", "")
    if ogrn:
        tag = "ОГРНИП" if entity_type == "ip" else "ОГРН"
        SubElement(contragent, tag).text = ogrn

    # Наименование
    name = fields.get("name", "")
    SubElement(contragent, "Наименование").text = name

    full_name = fields.get("full_name") or name
    SubElement(contragent, "ПолноеНаименование").text = full_name

    # Юридический адрес
    address = fields.get("address", "")
    if address:
        addr_el = SubElement(contragent, "ЮридическийАдрес")
        SubElement(addr_el, "Представление").text = address

    # Руководитель (только для ЮЛ)
    if entity_type != "ip":
        mgr_name = fields.get("management_name", "")
        mgr_post = fields.get("management_post", "")
        if mgr_name:
            contact = SubElement(contragent, "Контакт")
            SubElement(contact, "Тип").text = "Руководитель"
            SubElement(contact, "ФИО").text = mgr_name
            if mgr_post:
                SubElement(contact, "Должность").text = mgr_post

    # ОКВЭД
    okved = fields.get("okved_code", "")
    if okved:
        SubElement(contragent, "ОКВЭД").text = okved
        okved_text = fields.get("okved_text", "")
        if okved_text:
            SubElement(contragent, "ОписаниеОКВЭД").text = okved_text

    # Статус
    status = fields.get("status", "")
    if status:
        SubElement(contragent, "Статус").text = _status_text(status)

    # Тип
    SubElement(contragent, "ТипКонтрагента").text = (
        "ИндивидуальныйПредприниматель" if entity_type == "ip"
        else "ЮридическоеЛицо"
    )

    # Уставный капитал
    if entity_type != "ip" and fields.get("capital_value") is not None:
        SubElement(contragent, "УставныйКапитал").text = str(fields["capital_value"])

    # Дата регистрации
    reg = fields.get("registration_date", "")
    if reg:
        SubElement(contragent, "ДатаРегистрации").text = reg

    # XML declaration + content
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_bytes += tostring(root, encoding="unicode").encode("utf-8")

    return xml_bytes


def _now_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _status_text(status: str) -> str:
    return {
        "ACTIVE": "Действующая",
        "LIQUIDATING": "В стадии ликвидации",
        "LIQUIDATED": "Ликвидирована",
        "BANKRUPT": "Банкрот",
        "REORGANIZING": "Реорганизация",
    }.get(status, status)

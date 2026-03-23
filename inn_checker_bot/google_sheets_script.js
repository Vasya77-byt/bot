// ============================================================
//  Google Apps Script — проверка контрагента по ИНН (DaData)
//  Вставляется в Google Sheets: Расширения → Apps Script
// ============================================================

// ===== НАСТРОЙКИ =====
var DADATA_API_KEY = "3153003301db7b2210b573fc8500d890e38215af";
var SHEET_NAME     = "";        // Имя листа. Пусто = любой лист
var HEADER_ROWS    = 1;         // Сколько строк-заголовков пропускать

// ===== СТОЛБЦЫ (номер столбца, A=1, B=2, ...) =====
var COL_INN        = 5;   // E — ИНН (вводим сюда)
var COL_COMPANY    = 3;   // C — Компания
var COL_STATUS     = 11;  // K — Статус
var COL_REG_DATE   = 10;  // J — Регистрация
var COL_CITY       = 6;   // F — Юр. адрес (город)
var COL_OKVED      = 9;   // I — Осн. ОКВЭД
var COL_CAPITAL    = 4;   // D — Уст. капитал
var COL_FINANCE    = 7;   // G — Финансы (доходы)
var COL_STAFF      = 8;   // H — Штат
var COL_TAXES      = 12;  // L — Налоги (система налогообложения)

// ===== МАППИНГ СТАТУСОВ =====
var STATUS_MAP = {
  "ACTIVE":        "Действующая",
  "LIQUIDATING":   "Ликвидируется",
  "LIQUIDATED":    "Ликвидирована",
  "BANKRUPT":      "Банкрот",
  "REORGANIZING":  "Реорганизация"
};

// ===== МАППИНГ НАЛОГОВЫХ СИСТЕМ =====
var TAX_MAP = {
  "OSNO":    "ОСНО",
  "USN6":    "УСН 6%",
  "USN15":   "УСН 15%",
  "USN":     "УСН",
  "ENVD":    "ЕНВД",
  "ESHN":    "ЕСХН",
  "PATENT":  "Патент"
};


// ============================================================
//  ТРИГГЕР: вызывается при редактировании ячейки
// ============================================================
function onEdit(e) {
  if (!e || !e.range) return;

  var sheet = e.range.getSheet();
  var row   = e.range.getRow();
  var col   = e.range.getColumn();

  // Проверяем имя листа (если задано)
  if (SHEET_NAME && sheet.getName() !== SHEET_NAME) return;

  // Проверяем что изменена ячейка ИНН и это не заголовок
  if (col !== COL_INN || row <= HEADER_ROWS) return;

  var inn = String(e.value || "").trim();
  if (!inn) return;

  // Валидация ИНН (10 цифр для юрлиц)
  if (!/^\d{10}$/.test(inn)) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "ИНН должен содержать 10 цифр", "Ошибка", 3
    );
    return;
  }

  // Запрашиваем данные
  try {
    var data = fetchDaData(inn);
    if (!data) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        "Компания не найдена по ИНН " + inn, "Не найдено", 3
      );
      return;
    }
    fillRow(sheet, row, data);
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "Данные загружены для ИНН " + inn, "Готово", 2
    );
  } catch (err) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "Ошибка: " + err.message, "Ошибка", 5
    );
    Logger.log("Ошибка для ИНН " + inn + ": " + err);
  }
}


// ============================================================
//  ЗАПРОС К DaData
// ============================================================
function fetchDaData(inn) {
  var url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party";

  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "Token " + DADATA_API_KEY,
      "Accept": "application/json"
    },
    payload: JSON.stringify({ query: inn, count: 1 }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 403) {
    throw new Error("Ошибка авторизации DaData. Проверьте API-ключ.");
  }
  if (code !== 200) {
    throw new Error("DaData HTTP " + code);
  }

  var json = JSON.parse(response.getContentText());
  var suggestions = json.suggestions || [];

  if (suggestions.length === 0) return null;

  return suggestions[0].data;
}


// ============================================================
//  ЗАПОЛНЕНИЕ СТРОКИ
// ============================================================
function fillRow(sheet, row, d) {
  // Компания
  var name = safeGet(d, "name", "short_with_opf")
          || safeGet(d, "name", "full_with_opf")
          || "";
  sheet.getRange(row, COL_COMPANY).setValue(name);

  // Статус
  var statusCode = safeGet(d, "state", "status") || "";
  var statusText = STATUS_MAP[statusCode] || statusCode || "н/д";
  sheet.getRange(row, COL_STATUS).setValue(statusText);

  // Регистрация (дата + возраст)
  var regTs = safeGet(d, "state", "registration_date");
  var regText = "н/д";
  if (regTs) {
    var regDate = new Date(regTs);
    var dd = padZero(regDate.getDate());
    var mm = padZero(regDate.getMonth() + 1);
    var yyyy = regDate.getFullYear();
    regText = dd + "." + mm + "." + yyyy;

    // Возраст компании
    var now = new Date();
    var ageYears = ((now - regDate) / (365.25 * 24 * 60 * 60 * 1000)).toFixed(1);
    regText += " (" + ageYears + " лет)";
  }
  sheet.getRange(row, COL_REG_DATE).setValue(regText);

  // Юр. адрес — только город
  var city = safeGet(d, "address", "data", "city")
          || safeGet(d, "address", "data", "region")
          || "н/д";
  sheet.getRange(row, COL_CITY).setValue(city);

  // Осн. ОКВЭД
  var okved = d.okved || "";
  var okvedText = okved;
  // Ищем название основного ОКВЭД
  var okveds = d.okveds || [];
  for (var i = 0; i < okveds.length; i++) {
    if (okveds[i] && okveds[i].main) {
      okvedText = (okveds[i].code || okved) + " — " + (okveds[i].name || "");
      break;
    }
  }
  if (!okvedText && okveds.length > 0 && okveds[0]) {
    okvedText = (okveds[0].code || "") + " — " + (okveds[0].name || "");
  }
  sheet.getRange(row, COL_OKVED).setValue(okvedText || "н/д");

  // Уставный капитал
  var capital = safeGet(d, "capital", "value");
  sheet.getRange(row, COL_CAPITAL).setValue(
    capital != null ? formatMoney(capital) : "н/д"
  );

  // Финансы (доходы)
  var finance = d.finance || {};
  var income = finance.income;
  var expense = finance.expense;
  var finYear = finance.year;
  var finParts = [];
  if (income != null) finParts.push("доходы " + formatMoney(income));
  if (expense != null) finParts.push("расходы " + formatMoney(expense));
  var finText = finParts.length > 0 ? finParts.join(", ") : "нет данных";
  if (finYear && finParts.length > 0) finText += " (" + finYear + ")";
  sheet.getRange(row, COL_FINANCE).setValue(finText);

  // Штат
  var emp = d.employee_count;
  sheet.getRange(row, COL_STAFF).setValue(
    emp != null ? emp + " чел." : "нет данных"
  );

  // Налоги (система налогообложения)
  var taxCode = (finance.tax_system || "");
  var taxText = TAX_MAP[taxCode] || taxCode || "н/д";
  sheet.getRange(row, COL_TAXES).setValue(taxText);
}


// ============================================================
//  УТИЛИТЫ
// ============================================================

/** Безопасное получение вложенного поля */
function safeGet(obj /*, key1, key2, ... */) {
  var current = obj;
  for (var i = 1; i < arguments.length; i++) {
    if (current == null || typeof current !== "object") return null;
    current = current[arguments[i]];
  }
  return current != null ? current : null;
}

/** Форматирование денег: 10000 → "10 000 ₽" */
function formatMoney(v) {
  if (v == null) return "н/д";
  var n = Number(v);
  if (isNaN(n)) return String(v);
  // Разделяем тысячи пробелами
  var parts = Math.round(n).toString().split(".");
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return parts[0] + " ₽";
}

/** Дополнение нулём: 5 → "05" */
function padZero(n) {
  return n < 10 ? "0" + n : "" + n;
}


// ============================================================
//  РУЧНОЙ ЗАПУСК: проверить все ИНН на листе
// ============================================================
function checkAllINNs() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var lastRow = sheet.getLastRow();

  for (var row = HEADER_ROWS + 1; row <= lastRow; row++) {
    var inn = String(sheet.getRange(row, COL_INN).getValue() || "").trim();
    if (!/^\d{10}$/.test(inn)) continue;

    // Пропускаем если компания уже заполнена
    var existing = sheet.getRange(row, COL_COMPANY).getValue();
    if (existing) continue;

    try {
      var data = fetchDaData(inn);
      if (data) {
        fillRow(sheet, row, data);
        Logger.log("OK: ИНН " + inn);
      }
    } catch (err) {
      Logger.log("Ошибка ИНН " + inn + ": " + err);
    }

    // Пауза чтобы не превысить лимит DaData
    Utilities.sleep(500);
  }

  SpreadsheetApp.getActiveSpreadsheet().toast("Проверка завершена!", "Готово", 3);
}


// ============================================================
//  УСТАНОВКА ТРИГГЕРА (запустить 1 раз)
// ============================================================
function installTrigger() {
  // Удаляем старые триггеры onEdit
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "onEdit") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }

  // Создаём installable trigger
  ScriptApp.newTrigger("onEdit")
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();

  SpreadsheetApp.getActiveSpreadsheet().toast(
    "Триггер установлен! Теперь вводите ИНН в столбец E.",
    "Готово", 5
  );
}

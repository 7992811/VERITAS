"""Parse explicit owner parameters; never select a market price for the owner."""
import re
import uuid

COMMANDS = frozenset(("/currency_manual", "/currency_manual_close"))
HELP = ("Ручной режим CNYRUBf · риск до стопа с издержками до 15% капитала\n"
        "Открытие или добор:\n/currency_manual SELL КОНТРАКТЫ ЛИМИТ СТОП ЦЕЛЬ МИНУТЫ\n"
        "Для покупки замените SELL на BUY. Укажите свои цены цифрами; МИНУТЫ — ожидаемый срок "
        "для расчёта издержек, целое положительное число. Автоматического закрытия по времени нет.\n"
        "При доборе укажите стоп и цель текущей ручной позиции.\n"
        "Лимитная заявка ожидает исполнения по вашей цене до конца торгового дня.\n"
        "Закрытие ручной позиции:\n/currency_manual_close ЛИМИТ\n"
        "Команда готовит предложение. Отправка брокеру потребует кнопки подтверждения в отдельном сообщении.")


def parse(command, arguments, *, bot_id, owner_id, message_id):
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("MANUAL_MESSAGE_ID_REQUIRED")
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"veritas:owner-manual:{bot_id}:{owner_id}:{message_id}"))
    count = 1 if command == "/currency_manual_close" else 6
    if command not in COMMANDS or len(arguments) != count:
        raise ValueError("MANUAL_PARAMETERS_REQUIRED")
    if count == 1:
        result = {"request_id": request_id, "action": "CLOSE", "limit_price": arguments[0]}
    else:
        side, lots, limit, stop, target, minutes = arguments
        if side.upper() not in ("BUY", "SELL") or not re.fullmatch(r"[0-9]{1,9}", lots) or not re.fullmatch(r"[0-9]{1,9}", minutes):
            raise ValueError("MANUAL_PARAMETERS_REQUIRED")
        if int(minutes) < 1 or int(lots) < 1:
            raise ValueError("MANUAL_HOLD_MINUTES_REQUIRED")
        result = {"request_id": request_id, "action": "OPEN", "side": side.upper(), "lots": int(lots),
                  "limit_price": limit, "stop_price": stop, "target_price": target, "hold_minutes": int(minutes)}
    for key in ("limit_price", "stop_price", "target_price"):
        if key in result:
            value = result[key].replace(",", ".")
            if not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,9})?", value):
                raise ValueError("MANUAL_PARAMETERS_REQUIRED")
            result[key] = value
    return result

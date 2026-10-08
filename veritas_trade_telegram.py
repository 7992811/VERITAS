"""Private Telegram delivery of immutable Currency proposals.

This module never calls a broker. The existing bot remains the sole getUpdates
consumer; durable decisions are recorded before its offset can advance.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx
import veritas_currency_manual_telegram as MANUAL_TG

EXPECTED_BOT_USERNAME = "axednewsi_bot"
PREFIX = "/internal/currency-trading/"
MSK = ZoneInfo("Europe/Moscow")
OPERATOR_COMMANDS = frozenset(("/currency_status", "/currency_bind")) | MANUAL_TG.COMMANDS
REASONS = {
    "MANUAL_PARAMETERS_REQUIRED": "Укажите направление, 1 контракт, лимитную цену, стоп, цель и ожидаемый срок: /currency_manual.",
    "MANUAL_TRIAL_ONE_CONTRACT_ONLY": "Ручная пробная заявка поддерживает ровно 1 контракт.",
    "MANUAL_TRIAL_REQUIRES_FLAT_POSITION": "Ручное открытие доступно при нулевой позиции CNYRUBf; имеющуюся ручную позицию можно закрыть: /currency_manual_close.",
    "MANUAL_TRIAL_REQUIRES_FLAT_WHOLE_ACCOUNT": "Пробный ручной режим требует отсутствия других позиций и заявок на всём брокерском счёте.",
    "MANUAL_POSITION_REQUIRED": "Нет учтённой ручной позиции из одного контракта для закрытия.",
    "MANUAL_GEOMETRY_INVALID": "Для покупки: стоп ниже лимита, цель выше. Для продажи: цель ниже лимита, стоп выше.",
    "MANUAL_APPROVED_PRICE_OFF_TICK": "Одна из цен не соответствует шагу цены брокерского контракта.",
    "MANUAL_LEVEL_ALREADY_REACHED": "Текущая цена уже достигла указанного стопа или цели; требуется другое предложение.",
    "MANUAL_ECONOMICS_BLOCKED": "Указанные уровни не проходят действующие проверки издержек и соотношения дохода к риску.",
    "MANUAL_CURRENT_ACCOUNT_NOT_CHECKED": "Текущее состояние счёта для ручной заявки ещё не проверено.",
    "MANUAL_CURRENT_ACCOUNT_POLICY_REQUIRED": "Настройки допуска ручной заявки не согласованы; требуется проверка конфигурации.",
    "MANUAL_PLAN_VERSION_REQUIRED": "Условия ручного режима обновлены. Создайте новое предложение и подтвердите его отдельно.",
    "MANUAL_HOLD_MINUTES_REQUIRED": "Укажите ожидаемый срок для расчёта издержек от 1 до 60 минут.",
    "MANUAL_INTENT_EXPIRED": "Срок ручного предложения истёк. Новая команда потребует нового подтверждения.",
    "MANUAL_REQUEST_ID_REUSED": "Эта команда уже связана с другим предложением; повторная отправка запрещена.",
    "PRICE_OUTSIDE_APPROVED_LIMIT": "Текущая цена не укладывается в ваш лимит. Заявка FAK по этим условиям не подготовлена.",
    "LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED": "Для реальной заявки не подтверждены история капитала и риск-лимиты всего брокерского счёта.",
    "SINGLE_ASSET_LIMIT": "Один контракт превышает действующий лимит доли актива от капитала всего счёта.",
    "STOP_RISK_LIMIT": "Риск до стопа с издержками превышает лимит всего счёта.",
    "STOP_RISK_CHANGED_AFTER_APPROVAL": "Расчётный риск до стопа с издержками превышает лимит валютного портфеля.",
    "ECONOMICS_CHANGED_AFTER_APPROVAL": "Условия после учёта издержек больше не проходят проверку дохода к риску.",
    "PROPOSALS_PAUSED": "Подготовка предложений приостановлена в настройках валютного портфеля.",
    "INSUFFICIENT_MARGIN_AFTER_APPROVAL": "Свободных средств недостаточно для ГО и комиссии заявки.",
    "MARGIN_INCREASE_REQUIRES_NEW_APPROVAL": "ГО контракта увеличилось; требуется новое предложение.",
    "BROKER_LOT_LIMIT_CHANGED": "Текущий лимит брокера не позволяет указанный объём.",
    "ALLOCATION_EXPOSURE_LIMIT_CHANGED": "Номинал позиции превышает разрешённое плечо валютного портфеля.",
    "DISABLED": "Предложения сделок отключены в конфигурации.",
    "CURRENCY_ACCOUNT_NOT_BOUND": "Счёт ещё не привязан к учёту валютного портфеля.",
    "EXECUTION_DISABLED": "Отправка заявок брокеру отключена; подтверждения только сохраняются.",
    "CURRENCY_TRADE_EXECUTION_DISABLED": "Отправка заявок брокеру отключена; подтверждения только сохраняются.",
    "LIVE_ACCOUNT_ADMISSION_REQUIRED": "Не подтверждён риск-допуск модели и всего реального счёта.",
    "RECONCILIATION_PENDING": "Ожидается сверка заявки, исполнений и комиссий с брокером.",
    "EXECUTION_RECONCILIATION_PENDING": "Ожидается сверка заявки, исполнений и комиссий с брокером.",
    "NO_CANONICAL_EVENT": "Сейчас нет нового подтверждённого сигнала.",
    "VERIFIED_FLAT_BROKER_ACCOUNT_REQUIRED": "Для первой привязки нужны нулевая позиция CNYRUBf и отсутствие активных заявок.",
    "CURRENCY_BIND_REQUIRES_FLAT_ACCOUNT": "Для первой привязки нужны нулевая позиция CNYRUBf и отсутствие активных заявок.",
    "TRADE_BOT_BINDING_MISMATCH": "Идентификатор бота не совпадает с настройкой торгового сервиса.",
    "TRADE_REQUEST_SCOPE_MISMATCH": "Настройка счёта или окружения изменилась; запросите состояние заново.",
    "PRIVATE_TRADE_OWNER_REQUIRED": "Владелец личного чата не совпадает с настройкой торгового сервиса.",
    "TRADE_REQUEST_REJECTED": "Сервис отклонил запрос без распознанной причины. Причина требует диагностики.",
    "TRADE_SERVICE_UNAVAILABLE": "Торговый сервис временно недоступен; состояние не подтверждено.",
    "TRADE_SERVICE_AUTH_REQUIRED": "Ключ доступа бота не совпадает с ключом торгового сервиса.",
    "TRADE_SERVICE_KEY_NOT_CONFIGURED": "В торговом сервисе не настроен отдельный ключ доступа.",
    "EXPLICIT_TRADE_OWNER_CONFIGURATION_REQUIRED": "В сервисе не завершена настройка владельца, личного чата или бота.",
    "DISTINCT_TRADE_SERVICE_AND_APPROVAL_KEYS_REQUIRED": "Для сервиса и подписи подтверждений нужны разные настроенные ключи.",
    "EXACT_OPEN_FULL_ACCESS_ACCOUNT_REQUIRED": "Выбранный брокерский счёт должен быть открыт и доступен этому токену с правом торговли.",
    "FLAT_UNENCUMBERED_10000_RUB_REQUIRED": "Для первой привязки нужны свободные 10 000 ₽, нулевая позиция CNYRUBf и отсутствие активных заявок.",
    "BROKER_LIMITS_NOT_READY": "Брокер ещё не подтвердил текущие лимиты; повторите проверку позже.",
    "BROKER_POSITION_MISMATCH": "Позиция брокера отличается от отдельного учёта Currency; требуется сверка.",
    "WORKING_ORDER_RECONCILIATION_REQUIRED": "Есть активная заявка; сначала нужна сверка её исполнения.",
    "FUNDING_COMPLETENESS_UNVERIFIED": "Полнота учёта фондирования ещё не подтверждена; новые входы блокируются.",
    "BROKER_COST_RECONCILIATION_REQUIRED": "Не завершена сверка фактических комиссий и фондирования.",
    "BROKER_FACTS_UNAVAILABLE": "Свежие брокерские данные недоступны; готовность не подтверждена.",
    "EXECUTION_QUOTE_STALE": "Котировка исполнения устарела; требуется свежая цена того же источника и контракта.",
    "BROKER_QUOTE_STALE": "Биржевой снимок стакана у брокера старше допустимого срока; ожидается свежий снимок.",
    "SAME_TF_CONTEXT_STALE": "Свечи выбранного таймфрейма устарели; для проверки входа нужен обновлённый контекст.",
    "SAME_TF_DIRECTION_CONFLICT": "Направление аналитического сигнала не совпадает с подтверждённым пробоем.",
    "R59_EXECUTION_TF_DIRECTION_CONFLICT": "Направление сигнала противоречит структуре выбранного таймфрейма.",
    "SAME_TF_CONFIRMATION_BREAKS_STRUCTURE": "Последующие свечи нарушили структуру исходного сигнала; вход не разрешён.",
    "NO_CURRENCY_CANDIDATE": "Сейчас нет направленного сигнала CNYRUBf для проверки входа.",
    "STRUCTURAL_BREAKOUT_LEVEL_NOT_HELD": "Цена больше не удерживает уровень пробоя; текущий сигнал не разрешает вход.",
    "STRUCTURAL_TARGET_ALREADY_REACHED": "Исходная цель движения уже достигнута; для входа нужен новый сигнал.",
    "STRUCTURAL_EVENT_EXPIRED": "Срок действия сигнала истёк; свежая котировка не продлевает старый сигнал.",
    "TRADE_SERVICE_TEMPORARILY_UNAVAILABLE": "Торговый сервис временно недоступен; состояние не подтверждено.",
    "LIVE_EVIDENCE_ISSUER_NOT_CONFIGURED": "Не настроен источник подтверждённых данных для риск-допуска реального счёта.",
    "LIVE_ACCOUNT_ADMISSION_NOT_CHECKED": "Риск-допуск реального счёта ещё не проверен; новые входы недоступны.",
    "LIVE_ACCOUNT_ADMISSION_STALE": "Данные риск-допуска реального счёта устарели; требуется свежая проверка.",
    "LIVE_ACCOUNT_CONTROLS_EVIDENCE_REQUIRED": "Не подтверждены текущие ограничения и риск-показатели всего брокерского счёта.",
    "LIVE_MODEL_ADMISSION_EVIDENCE_REQUIRED": "Не подтверждён допуск модели к реальным сделкам.",
    "LIVE_MODEL_EVIDENCE_NOT_CHECKED": "Допуск модели к реальным сделкам ещё не проверен.",
    "LIVE_ACCOUNT_HISTORY_EVIDENCE_NOT_CHECKED": "История капитала и ограничения всего счёта ещё не проверены.",
    "FUNDING_HISTORY_REFRESH_REQUIRED": "Нужно обновить историю фактического фондирования перед новым входом.",
    "FUNDING_HISTORY_INCOMPLETE": "История фактического фондирования неполна; новые входы заблокированы до сверки.",
    "STATEMENT_ATTESTATION_NOT_CONFIGURED": "Не настроен источник подтверждения полноты брокерского отчёта.",
    "FUNDING_STATEMENT_ATTESTATION_REQUIRED": "Нужно подтверждение полноты брокерского отчёта по фондированию.",
    "SETTLEMENT_HISTORY_CORRECTED": "Брокер исправил историю расчётов; перед новым входом требуется повторная сверка учёта.",
    "SETTLEMENT_CHECKPOINT_DUE": "Наступил срок очередной сверки брокерских расчётов.",
    "TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT": "Плановый объём уже набран либо допустимого размера недостаточно для одного целого контракта.",
    "INSUFFICIENT_MARGIN_FOR_ONE_CONTRACT": "Свободных средств у брокера недостаточно для ГО одного контракта и комиссии входа.",
    "CURRENCY_MARGIN_BUDGET_EXCEEDED": "ГО позиции и комиссия входа превышают доступный капитал валютного портфеля.",
    "BROKER_LOT_LIMIT_BELOW_ONE_CONTRACT": "Брокер не разрешает ни одного контракта в выбранном направлении.",
    "ALLOCATION_EXPOSURE_LIMIT_EXCEEDED": "Номинал позиции превышает разрешённое плечо валютного портфеля.",
    "FINAL_CONTRACT_STOP_RISK_EXCEEDED": "Риск целого количества контрактов до стопа с издержками превышает лимит портфеля.",
    "PLAN_VERSION_REQUIRES_NEW_APPROVAL": "Расчёт условий обновлён; требуется новое предложение и подтверждение.",
}


def operator_command(text):
    parts = str(text or "").strip().split()
    if not parts:
        return None
    command, _, suffix = parts[0].partition("@")
    if suffix and suffix.lower() != EXPECTED_BOT_USERNAME:
        return None
    return command.lower() if command.lower() in OPERATOR_COMMANDS else None


def _reason(code):
    # Never put arbitrary HTTP bodies, account details or exception text in chat.
    if isinstance(code, str) and code in REASONS:
        return REASONS[code]
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        return "Ограничение: " + code + "."
    return "Состояние ещё не подтверждено."


def _manual_check(code, details):
    if not isinstance(code, str):
        return {}
    fields = {
        "MANUAL_ECONOMICS_BLOCKED": ("net_reward_risk", "minimum_reward_risk",
            "expected_move_pct", "minimum_expected_move_pct", "modeled_round_trip_cost_pct"),
        "STOP_RISK_CHANGED_AFTER_APPROVAL": ("stop_risk_rub", "stop_risk_limit_rub", "currency_nav_rub"),
    }.get(code, ())
    if not isinstance(details, dict):
        return {}
    return {key: details[key] for key in fields if isinstance(details.get(key), str)
            and re.fullmatch(r"-?\d{1,21}(?:\.\d{1,12})?", details[key])}


def manual_refusal_text(result):
    code = result.get("code") or result.get("error")
    lines = [_reason(code)]
    metrics = {k: Decimal(v) for k, v in _manual_check(code, result.get("manual_check")).items()}
    def shown(value, places=2):
        return f"{value:.{places}f}".replace(".", ",")
    if code == "MANUAL_ECONOMICS_BLOCKED":
        if all(k in metrics for k in ("net_reward_risk", "minimum_reward_risk")):
            lines.append("Доход / риск после расчётных издержек: " + shown(metrics["net_reward_risk"], 4)
                         + "; минимум: " + shown(metrics["minimum_reward_risk"], 4) + ".")
        if all(k in metrics for k in ("expected_move_pct", "minimum_expected_move_pct")):
            lines.append("Потенциал до цели: " + shown(100 * metrics["expected_move_pct"])
                         + "%; минимум по издержкам: " + shown(100 * metrics["minimum_expected_move_pct"]) + "%.")
    elif all(k in metrics for k in ("stop_risk_rub", "stop_risk_limit_rub", "currency_nav_rub")):
        lines.append("Риск с расчётными издержками: " + shown(metrics["stop_risk_rub"])
                     + " ₽; лимит: " + shown(metrics["stop_risk_limit_rub"]) + " ₽.")
        lines.append("Капитал валютного портфеля: " + shown(metrics["currency_nav_rub"]) + " ₽.")
    return "\n".join(lines)


def _sizing_lines(details):
    """Render only typed numeric diagnostics from the last completed poll."""
    if not isinstance(details, dict):
        return []
    try:
        numbers = {}
        for key in ("currency_nav_rub", "target_fraction", "target_notional_rub",
                    "contract_notional_rub", "max_gross"):
            value = details.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"\d{1,20}(?:\.\d{1,20})?", value):
                return []
            numbers[key] = Decimal(value)
            if numbers[key] <= 0:
                return []
        target, held = details.get("target_lots"), details.get("held_lots")
        if any(type(x) is not int or not 0 <= x <= 10**12 for x in (target, held)):
            return []
        n = numbers
        if (n["target_notional_rub"] != n["currency_nav_rub"] * n["target_fraction"]
                or target != int(n["target_notional_rub"] / n["contract_notional_rub"])
                or target > held or n["target_fraction"] > n["max_gross"]):
            return []
        reason = details.get("reason")
        if reason == "BELOW_ONE_CONTRACT" and target == 0:
            first = "Расчётный объём меньше одного целого контракта."
        elif reason == "TARGET_ALREADY_REACHED" and held >= 1:
            first = "Целевое количество контрактов уже набрано; добор не требуется."
        else:
            return []
        def rub(value):
            return format(value, ",.2f").replace(",", " ").replace(".", ",") + " ₽"
        return [first, "Расчёт последней проверки:",
                f"Капитал Currency: {rub(n['currency_nav_rub'])}.",
                f"Доля от капитала: {n['target_fraction'] * 100:f}% · номинал цели: {rub(n['target_notional_rub'])}.",
                f"Номинал одного контракта: {rub(n['contract_notional_rub'])} — это не гарантийное обеспечение.",
                f"Цель: {target} контракт(ов) · уже в позиции: {held}.",
                f"Предел номинала: {n['max_gross']:f}× капитала; он не умножает начальную долю автоматически."]
    except (InvalidOperation, ValueError, OverflowError):
        return []


def _entry_lines(details):
    """Show saved clocks and checked alternatives, without refreshing a signal."""
    horizons = {"1m", "5m", "1h", "4h", "1d", "3d", "7d"}
    if (not isinstance(details, dict) or not isinstance(details.get("horizon"), str)
            or details["horizon"] not in horizons or not isinstance(details.get("direction"), str)):
        return []
    direction = {"LONG": "покупка", "SHORT": "продажа"}.get(details.get("direction"))
    if direction is None:
        return []
    lines = [f"Проверенный сигнал: CNYRUBf · {details['horizon']} · {direction}."]
    if details.get("checked_at"):
        lines.append("Проверка сигнала: " + _stamp(details["checked_at"]))
    if details.get("context_closed_at"):
        lines.append("Последняя закрытая свеча: " + _stamp(details["context_closed_at"]))
    age, limit = details.get("context_age_seconds"), details.get("context_max_age_seconds")
    if (type(age) in (int, float) and 0 <= age <= 10**9
            and type(limit) is int and 0 < limit <= 604800):
        lines.append(f"Возраст свечи при проверке: {age:.0f} с; допустимо: {limit} с.")
    if details.get("quote_observed_at"):
        lines.append("Котировка при проверке: " + _stamp(details["quote_observed_at"]))
    routes = details.get("routes")
    for item in routes[:7] if isinstance(routes, list) else []:
        if (isinstance(item, dict) and item.get("selected") is False
                and isinstance(item.get("horizon"), str) and item["horizon"] in horizons):
            lines.append(f"Другой вариант, {item['horizon']}: " + _reason(item.get("reason")))
    return lines


def readiness_text(status):
    """Describe metadata only; a status request never polls or executes a trade."""
    lines = ["Валютный портфель · состояние"]
    if status.get("ok") is not True:
        return "\n".join(lines + [_reason(status.get("code") or status.get("error"))])
    if status.get("enabled") is not True:
        return "\n".join(lines + [REASONS["DISABLED"], "Привязка и отправка заявок этой командой не включаются."])
    environment = status.get("execution_environment")
    lines.append("Окружение: " + {"production": "реальный счёт", "sandbox": "песочница"}.get(environment, "не определено"))
    account = str(status.get("account_id") or "")
    lines.append("Счёт: …" + account[-4:] if account else "Счёт: не определён")
    lines.append("Отправка брокеру: " + ("разрешена конфигурацией; только после отдельного подтверждения"
                 if status.get("execution_enabled") is True else "отключена"))
    binding = status.get("binding_state", "unchecked")
    lines.append("Привязка учёта: " + {"bound": "подтверждена", "unbound": "не выполнена",
                 "unchecked": "ещё не проверена"}.get(binding, "ещё не проверена"))
    if status.get("new_risk_block_reason"):
        lines.append("Входы по сигналам модели: " + _reason(status["new_risk_block_reason"]))
    manual = status.get("manual_account_admission")
    if isinstance(manual, dict):
        lines.append("Ручные заявки: /currency_manual · закрытие: /currency_manual_close")
        if manual.get("account_history_required") is False:
            lines.append("История капитала всего счёта для ручного режима не требуется.")
        blockers = manual.get("blockers") or []
        if blockers:
            lines.append("Проверка ручного режима: " + _reason(blockers[0]))
    if status.get("last_poll_at"):
        lines.append("Последняя проверка: " + _stamp(status["last_poll_at"]))
        if status.get("status_stale") is True:
            lines.append("Данные проверки устарели; текущая готовность не подтверждена.")
        if status.get("last_poll_succeeded") is False:
            lines.append("Последняя проверка завершилась ошибкой.")
        if status.get("last_poll_block_reason"):
            sizing = (_sizing_lines(status.get("last_poll_sizing"))
                      if status["last_poll_block_reason"] == "TARGET_ALREADY_REACHED_OR_BELOW_ONE_CONTRACT" else [])
            lines.extend(sizing or [_reason(status["last_poll_block_reason"])])
            lines.extend(_entry_lines(status.get("last_poll_entry_diagnostics")))
        for key, label in (("pending_approval_count", "Ожидают решения/доставки"),
                           ("unsettled_count", "Ожидают сверки исполнения")):
            value = status.get(key)
            if type(value) is int and value >= 0:
                lines.append(f"{label}: {value}")
        lines.append("Это результат последней проверки, а не новая проверка брокерского счёта.")
    else:
        lines.append("Проверка сигналов и брокерского состояния ещё не завершалась; готовность не подтверждена.")
    lines.append("/currency_bind — явная первоначальная привязка учёта. Торговые флаги не изменяет.")
    return "\n".join(lines)


class TradeTelegramError(RuntimeError):
    pass


def _positive(value):
    if isinstance(value, bool):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED") from None
    if result <= 0 or str(result) != str(value):
        raise TradeTelegramError("EXPLICIT_TRADE_OWNER_REQUIRED")
    return result


def _stamp(value):
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S МСК")
    except (ValueError, TypeError):
        return "срок не определён"


def proposal_text(proposal, *, execution_enabled=False):
    t = proposal["terms"]
    manual = t.get("decision_authority") == "OWNER_MANUAL_TRIAL_V1"
    manual_position = t.get("position_decision_authority") == "OWNER_MANUAL_TRIAL_V1"
    action = {"OPEN": "Открытие", "ADD": "Увеличение", "REDUCE": "Сокращение",
              "CLOSE": "Закрытие"}.get(t.get("action"), "Изменение")
    side = {"BUY": "покупка", "SELL": "продажа"}.get(t.get("side"), "?")
    reason = {"STRUCTURAL_STOP_REACHED": "цена достигла структурного стопа",
              "OWNER_MANUAL_CLOSE": "ручное закрытие по запросу владельца",
              "STRATEGY_TARGET_REACHED": "цена достигла цели",
              "CONFIRMED_OPPOSITE_CANONICAL_EVENT": "подтверждён противоположный сигнал",
              "CURRENCY_DRAWDOWN_LIMIT": "достигнут лимит просадки валютного портфеля"}
    account = str(t.get("account_id") or "")
    lines = [
        ("Валютный портфель · ручная заявка владельца" if manual else
         "Валютный портфель · выход из ручной позиции" if manual_position else
         "Валютный портфель · предложение сделки"),
        f"{action} CNYRUBf · {t.get('direction')}",
        f"Счёт: …{account[-4:]}",
        f"{side.capitalize()} · {t.get('lots')} контракт(ов)",
        f"Лимитная цена: {t.get('limit_price')} ₽",
        "Исполнение FAK: доступный объём сразу, остаток отменяется.",
        f"Номинал заявки: {t.get('order_notional_rub', '—')} ₽",
    ]
    if manual or manual_position:
        lines.append("Направление и уровни заданы владельцем. Рекомендация модели не использована.")
        if manual and t.get("plan_version") == "currency-owner-manual-v2-current-account":
            lines.append("Проверяется текущее состояние счёта. Историческая просадка и дневной/недельный результат всего счёта не проверяются.")
        if t.get("expected_hold_seconds"):
            lines.append(f"Ожидаемый срок для расчёта издержек: {Decimal(t['expected_hold_seconds']) / 60:g} мин; автоматического закрытия по времени нет.")
    elif t.get("horizon"):
        lines.append(f"Таймфрейм: {t['horizon']}")
    if t.get("stop_price") is not None:
        lines.append(f"{'Стоп владельца' if manual or manual_position else 'Уровень стопа стратегии'}: {t['stop_price']} ₽")
    if t.get("target_price") is not None:
        lines.append(f"{'Цель владельца' if manual or manual_position else 'Цель стратегии'}: {t['target_price']} ₽")
    if t.get("action") in ("OPEN", "ADD"):
        if t.get("sizing_mode") == "INITIAL_MINIMUM_CONTRACT":
            lines.append("Начальный объём: 1 целый контракт; расчётная доля стратегии меньше контракта.")
        if t.get("resulting_leverage") is not None:
            lines.append(f"Плечо всей позиции после заявки: {t['resulting_leverage']}× капитала портфеля.")
        lines.extend([
            f"Требуемое ГО: {t.get('required_margin_rub', '—')} ₽",
            f"Оценка комиссии входа: {t.get('estimated_commission_rub', '—')} ₽",
            f"Риск всей позиции до стопа с издержками: {t.get('total_stop_risk_rub', '—')} ₽",
            f"Порог потенциала CNYRUBf: {t.get('canonical_cost_multiple', '1.1')}× издержек; минимум 0,19%.",
        ])
    if t.get("exit_reason"):
        lines.append("Причина: " + reason.get(t["exit_reason"], "подтверждённое сокращение риска"))
    lines.extend([
        f"Действует до: {_stamp(proposal.get('expires_at'))}",
        "Закрытие по стопу или цели потребует отдельного подтверждения.",
        "Защитные заявки на бирже этим подтверждением не устанавливаются.",
    ])
    if execution_enabled is True:
        mode = "песочницу брокера" if t.get("execution_environment") == "sandbox" else "реальный брокерский счёт"
        lines.append(f"Кнопка подтверждает только указанные условия и отправку в {mode}.")
    else:
        lines.append("Отправка брокеру отключена. Подтверждение разрешит эту заявку только до указанного срока, после включения исполнения и повторной проверки условий.")
    return "\n".join(lines)[:3900]


def execution_text(proposal):
    status = proposal.get("status")
    labels = {
        "APPROVED": "Подтверждение сохранено; исполнение ещё не подтверждено.",
        "SENDING": "Заявка передаётся брокеру.",
        "UNKNOWN": "Ответ брокера не определён; идёт сверка. Повторная заявка не отправляется.",
        "ACKNOWLEDGED": "Брокер принял заявку; позиция пока не подтверждена исполнениями.",
        "PARTIALLY_FILLED": "Заявка исполнена частично.",
        "FILLED": "Заявка исполнена.",
        "CANCELLED": "Оставшаяся часть заявки отменена.",
        "BROKER_REJECTED": "Брокер отклонил заявку.",
        "REJECTED": "Вы отклонили предложение.",
        "EXPIRED": "Срок подтверждения истёк.",
        "BLOCKED": "Условия изменились; заявка заблокирована.",
    }
    text = labels.get(status, "Статус предложения обновлён.")
    if status == "BLOCKED" and proposal.get("reason_code") == "LIVE_ACCOUNT_ADMISSION_REQUIRED":
        text = "Заявка заблокирована: нет независимого риск-допуска реального счёта."
    filled = proposal.get("filled_lots")
    if filled is not None:
        text += f"\nИсполнено: {filled} из {proposal.get('terms', {}).get('lots', '?')} контракт(ов)."
    if proposal.get("average_fill_price") is not None:
        text += f"\nСредняя цена исполнения: {proposal['average_fill_price']}."
    return text


class InternalTradeClient:
    OPERATIONS = frozenset(("status", "bind", "poll", "decision", "claim-delivery",
                            "delivered", "delivery-unknown", "updates", "prepare-manual"))

    def __init__(self, url, key, client=None):
        parsed = urlparse(str(url))
        try:
            allowed = (parsed.scheme == "https"
                and parsed.hostname == "veritas-intelligence-v1.onrender.com"
                and parsed.port in (None, 443) and parsed.path in ("", "/")
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
        if not allowed or not isinstance(key, str) or len(key.encode()) < 32:
            raise TradeTelegramError("INVALID_INTERNAL_TRADE_CONFIGURATION")
        self.url, self.key = str(url).rstrip("/"), key
        self.client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)

    def __call__(self, operation, payload):
        if operation not in self.OPERATIONS:
            raise TradeTelegramError("UNSUPPORTED_TRADE_OPERATION")
        try:
            response = self.client.post(self.url + PREFIX + operation, json=payload,
                                       headers={"X-Veritas-Trade-Key": self.key})
        except Exception:
            raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE") from None
        if 300 <= response.status_code < 400:
            raise TradeTelegramError("TRADE_SERVICE_UNAVAILABLE")
        if response.status_code >= 400:
            try:
                refusal = response.json()
                code = refusal.get("code") if isinstance(refusal, dict) else None
            except Exception:
                code = None
            safe_code = code if isinstance(code, str) and code in REASONS else None
            if response.status_code >= 500:
                raise TradeTelegramError(safe_code or "TRADE_SERVICE_UNAVAILABLE")
            result = {"ok": False, "error": "TRADE_REQUEST_REJECTED",
                      "code": safe_code or "TRADE_REQUEST_REJECTED"}
            if safe_code and response.status_code == 409:
                metrics = _manual_check(safe_code, refusal.get("manual_check"))
                if metrics:
                    result["manual_check"] = metrics
            return result
        try:
            data = response.json()
        except Exception:
            raise TradeTelegramError("INVALID_TRADE_SERVICE_RESPONSE") from None
        if not isinstance(data, dict):
            raise TradeTelegramError("INVALID_TRADE_SERVICE_RESPONSE")
        return data


class TradeTelegramBridge:
    def __init__(self, service, telegram, owner_user_id, logger=None, clock=None):
        self.service, self.telegram = service, telegram
        self.owner_user_id = _positive(owner_user_id)
        self.logger = logger or (lambda message: None)
        self.bot_id = None
        self.worker_id = "telegram-" + uuid.uuid4().hex
        self._identity_lock, self._poll_lock = threading.RLock(), threading.Lock()
        self._updated = {}
        self._clock = clock or time.monotonic
        self._bind_challenge = None

    def _private_owner(self, message):
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        return (type(sender.get("id")) is int and sender["id"] == self.owner_user_id
                and sender.get("is_bot") is not True and chat.get("type") == "private"
                and type(chat.get("id")) is int and chat["id"] == self.owner_user_id
                and not message.get("forward_origin") and not message.get("forward_date")
                and not message.get("is_automatic_forward"))

    def _operator_reply(self, text):
        self.telegram("sendMessage", {"chat_id": str(self.owner_user_id), "text": text[:4096],
                                      "disable_web_page_preview": "true"})

    def handle_message(self, message):
        command = operator_command(message.get("text"))
        if command is None:
            return False
        # Suppress these commands outside the exact private owner conversation.
        # News moderators and channel administrators do not inherit this role.
        if not self._private_owner(message):
            return True
        self._identity()
        try:
            status = self._request("status")
        except TradeTelegramError as exc:
            self._operator_reply(_reason(str(exc) if str(exc) in REASONS else "TRADE_SERVICE_UNAVAILABLE"))
            return True
        if command == "/currency_status" or status.get("ok") is not True or status.get("enabled") is not True:
            self._operator_reply(readiness_text(status))
            return True
        scope = {key: status.get(key) for key in ("account_id", "instrument_uid", "execution_environment")}
        if (not all(isinstance(value, str) and value for value in scope.values())
                or scope["execution_environment"] not in ("production", "sandbox")):
            self._operator_reply("Сервис не подтвердил точный счёт и окружение. Привязка недоступна.")
            return True
        arguments = str(message.get("text") or "").strip().split()[1:]
        if command in MANUAL_TG.COMMANDS:
            try:
                request = MANUAL_TG.parse(command, arguments, bot_id=self.bot_id,
                    owner_id=self.owner_user_id, message_id=message.get("message_id"))
            except ValueError:
                self._operator_reply(MANUAL_TG.HELP)
                return True
            try:
                result = self._request("prepare-manual", dict(scope, request=request,
                    sender_user_id=self.owner_user_id, private_chat_id=self.owner_user_id, chat_type="private"))
                proposal = result.get("proposal") or {}
                if result.get("ok") is True and proposal:
                    self._operator_reply("Ручная заявка подготовлена. Предложение с условиями и кнопками придёт отдельным сообщением."
                        if proposal.get("status") == "PENDING_DELIVERY" else execution_text(proposal))
                else:
                    self._operator_reply(manual_refusal_text(result))
            except TradeTelegramError:
                self._operator_reply("Ответ на подготовку не получен. Не повторяйте команду с новыми параметрами, "
                    "пока не проверите /currency_status и сообщения с предложениями.")
            return True
        if not arguments:
            nonce = uuid.uuid4().hex[:20]
            self._bind_challenge = {"nonce": nonce, "scope": scope, "expires": self._clock() + 120}
            mode = "РЕАЛЬНЫЙ СЧЁТ" if scope["execution_environment"] == "production" else "ПЕСОЧНИЦА"
            self._operator_reply(
                f"Первоначальная привязка · {mode} · счёт …{scope['account_id'][-4:]}\n"
                "Создаст отдельный учёт Currency с расчётной долей 10 000 ₽. "
                "Деньги не переводятся, заявки брокеру не отправляются, разрешения на торговлю не меняются.\n"
                "Сервис проверит точный счёт, доступные средства, нулевую позицию CNYRUBf и отсутствие активных заявок. "
                "Повторная привязка не обнуляет существующий учёт.\n"
                f"Если это выбранный вами счёт, подтвердите в течение 120 секунд:\n/currency_bind {nonce}")
            return True
        challenge = self._bind_challenge
        if (len(arguments) != 1 or not challenge or arguments[0] != challenge["nonce"]
                or self._clock() >= challenge["expires"] or scope != challenge["scope"]):
            self._bind_challenge = None
            self._operator_reply("Подтверждение привязки устарело или счёт изменился. Начните заново: /currency_bind")
            return True
        try:
            result = self._request("bind", dict(scope, sender_user_id=self.owner_user_id,
                private_chat_id=self.owner_user_id, chat_type="private"))
        except TradeTelegramError:
            # A lost response must not be described as a failed binding. Repeating
            # this exact request is safe: the ledger never resets an existing bind.
            self._operator_reply("Ответ на привязку не получен. Результат пока не подтверждён. "
                                 "Повторите ту же команду в пределах срока или проверьте /currency_status.")
            return True
        self._bind_challenge = None
        if result.get("ok") is True and (result.get("binding") or {}).get("status") == "BOUND":
            self._operator_reply("Учёт Currency привязан к выбранному счёту. Деньги не переведены, заявки не отправлены. "
                                 "Разрешения на торговлю не менялись. Состояние: /currency_status")
        else:
            self._operator_reply(_reason(result.get("code") or result.get("error")))
        return True

    def _identity(self):
        with self._identity_lock:
            if self.bot_id is None:
                me = self.telegram("getMe", {})
                if (not isinstance(me, dict) or me.get("is_bot") is not True
                        or str(me.get("username", "")).lower() != EXPECTED_BOT_USERNAME):
                    raise TradeTelegramError("WRONG_TRADE_BOT")
                self.bot_id = _positive(me.get("id"))
            return self.bot_id

    def _scope(self, proposal):
        return (proposal.get("owner_user_id") == self.owner_user_id
                and proposal.get("private_chat_id") == self.owner_user_id
                and proposal.get("bot_id") == self.bot_id)

    def _request(self, operation, payload=None):
        body = dict(payload or {}, bot_id=self._identity())
        return self.service(operation, body)

    def _answer(self, query_id, text):
        try:
            self.telegram("answerCallbackQuery", {"callback_query_id": query_id, "text": text})
        except Exception:
            pass

    def poll(self):
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            response = self._request("poll")
            for proposal in response.get("items") or []:
                if proposal.get("status") != "PENDING_DELIVERY" or not self._scope(proposal):
                    continue
                claimed_response = self._request("claim-delivery",
                    {"proposal_id": proposal["proposal_id"], "worker_id": self.worker_id})
                claimed = claimed_response.get("proposal")
                if (not claimed or not self._scope(claimed)
                        or claimed.get("status") != "DELIVERY_SENDING" or not claimed.get("delivery_token")):
                    continue
                proposal_id, lease = claimed["proposal_id"], claimed.get("delivery_token")
                try:
                    callbacks = claimed.get("callbacks") or {}
                    approve = claimed.get("approve_callback") or callbacks.get("approve")
                    reject = claimed.get("reject_callback") or callbacks.get("reject")
                    if not approve or not reject:
                        raise TradeTelegramError("MISSING_SIGNED_BUTTONS")
                    sent = self.telegram("sendMessage", {
                        "chat_id": str(self.owner_user_id),
                        "text": proposal_text(claimed, execution_enabled=response.get("execution_enabled") is True),
                        "disable_web_page_preview": "true",
                        "reply_markup": json.dumps({"inline_keyboard": [[
                            {"text": "Подтверждаю", "callback_data": approve},
                            {"text": "Отклонить", "callback_data": reject}]]}, ensure_ascii=False),
                    })
                    if (not isinstance(sent, dict) or sent.get("chat", {}).get("type") != "private"
                            or sent.get("chat", {}).get("id") != self.owner_user_id
                            or type(sent.get("message_id")) is not int or sent["message_id"] <= 0):
                        raise TradeTelegramError("DELIVERY_IDENTITY_MISMATCH")
                except Exception:
                    self._request("delivery-unknown", {"proposal_id": proposal_id, "delivery_token": lease})
                    continue
                body = {"proposal_id": proposal_id, "private_chat_id": self.owner_user_id,
                        "message_id": sent["message_id"], "terms_hash": claimed["terms_hash"],
                        "delivery_token": lease}
                for attempt in range(2):
                    try:
                        acknowledged = self._request("delivered", body)
                        if acknowledged.get("ok") is True:
                            break
                    except TradeTelegramError:
                        if attempt == 1:
                            raise
            updates = self._request("updates")
            for proposal in updates.get("items") or []:
                message_id = proposal.get("telegram_message_id")
                if not self._scope(proposal) or not message_id:
                    continue
                if proposal.get("status") in ("PENDING_DELIVERY", "DELIVERY_SENDING", "DELIVERY_UNKNOWN", "AWAITING_OWNER"):
                    continue
                key = (proposal.get("status"), proposal.get("filled_lots"),
                       str(proposal.get("average_fill_price")), proposal.get("execution_reconciled"))
                if self._updated.get(proposal["proposal_id"]) == key:
                    continue
                try:
                    self.telegram("editMessageText", {"chat_id": str(self.owner_user_id),
                        "message_id": str(message_id), "text": proposal_text(proposal,
                            execution_enabled=updates.get("execution_enabled") is True) +
                            "\n\n" + execution_text(proposal),
                        "reply_markup": json.dumps({"inline_keyboard": []})})
                    self._updated[proposal["proposal_id"]] = key
                    if len(self._updated) > 500:
                        self._updated.pop(next(iter(self._updated)))
                except Exception:
                    pass
        finally:
            self._poll_lock.release()

    def handle_callback(self, query):
        data = query.get("data") or ""
        if not isinstance(data, str) or not data.startswith("ta:"):
            return False
        bot_id = self._identity()
        sender, message = query.get("from") or {}, query.get("message") or {}
        chat, origin = message.get("chat") or {}, message.get("from") or {}
        if (type(sender.get("id")) is not int or sender["id"] != self.owner_user_id
                or sender.get("is_bot") is True or chat.get("type") != "private"
                or chat.get("id") != self.owner_user_id or origin.get("id") != bot_id
                or origin.get("is_bot") is not True):
            self._answer(query.get("id"), "Подтверждение доступно только владельцу в личном чате.")
            return True
        response = self._request("decision", {"callback_data": data,
            "sender_user_id": sender["id"], "private_chat_id": chat["id"], "chat_type": "private",
            "message_id": message.get("message_id"), "callback_query_id": query.get("id")})
        result = response.get("proposal") or response
        status = result.get("status")
        text = {"APPROVED": "Подтверждение сохранено.", "REJECTED": "Предложение отклонено.",
                "EXPIRED": "Срок подтверждения истёк."}.get(status, "Предложение уже обработано или недоступно.")
        self._answer(query.get("id"), text)
        return True


def build_from_env(telegram, logger=None):
    if os.getenv("VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED", "").lower() not in ("1", "true", "yes", "on"):
        return None
    service = InternalTradeClient(
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_URL", "https://veritas-intelligence-v1.onrender.com"),
        os.getenv("VERITAS_CURRENCY_TRADE_SERVICE_KEY", ""))
    if os.getenv("VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED", "").lower() in ("1", "true", "yes", "on"):
        from veritas_currency_console_telegram import build
        return build(service, telegram, logger)
    return TradeTelegramBridge(service, telegram,
        _positive(os.getenv("VERITAS_CURRENCY_TRADE_OWNER_USER_ID")), logger)


def handle_operator_message(message, bridge, telegram):
    """Called by the one existing update consumer, including feature-off mode."""
    if operator_command(message.get("text")) is None:
        return False
    if bridge is None:
        try:
            owner = _positive(os.getenv("VERITAS_CURRENCY_TRADE_OWNER_USER_ID"))
        except TradeTelegramError:
            return True
        bridge = TradeTelegramBridge(lambda *_: {"ok": True, "enabled": False}, telegram, owner)
    return bridge.handle_message(message)


def start_worker(bridge, stop_event, logger=None):
    if bridge is None:
        return None
    log = logger or (lambda message: None)
    def run():
        while not stop_event.is_set():
            try:
                bridge.poll()
            except Exception as exc:
                log("Currency trade worker: " + type(exc).__name__)
            stop_event.wait(5)
    worker = threading.Thread(target=run, name="currency-trade-proposals", daemon=True)
    worker.start()
    return worker

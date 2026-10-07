"""Bounded, read-only CNYRUBf connectivity check for the T-Invest sandbox.

``run_probe(environment="sandbox")`` is explicit and import is inert. The only
credential source is TBANK_SANDBOX_TOKEN. The account must be supplied directly
or through TBANK_SANDBOX_ACCOUNT_ID; no account is selected automatically.
Neither production URLs nor sandbox mutations are part of the transport's
allowlist. A VERIFIED report proves only these reads, never order submission,
funding completeness, strategy admission, or real-account readiness.

Official contracts checked 2026-10-07:
https://developer.tbank.ru/invest/intro/developer/sandbox
https://developer.tbank.ru/invest/intro/developer/sandbox/url_difference
https://developer.tbank.ru/invest/intro/developer/sandbox/methods
https://developer.tbank.ru/invest/api/instruments-service-future-by
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import ssl
import time
from types import MappingProxyType
from uuid import UUID


VERSION = "CURRENCY_SANDBOX_READ_PROBE_V1"
CNYRUBF_UID = "c300543d-aa18-4249-b110-615409dde036"
SANDBOX_ROOT = "https://sandbox-invest-public-api.tbank.ru/rest/"
PACKAGE = "tinkoff.public.invest.api.contract.v1."
READ_ROUTES = MappingProxyType({
    "accounts": "SandboxService/GetSandboxAccounts",
    "future": "InstrumentsService/FutureBy",
    "portfolio": "SandboxService/GetSandboxPortfolio",
    "orders": "SandboxService/GetSandboxOrders",
})
ALLOWED_URLS = frozenset(SANDBOX_ROOT + PACKAGE + route for route in READ_ROUTES.values())
MAX_RESPONSE_BYTES = 262_144
MAX_ROWS = 1000
INT64_MAX = 2**63 - 1

DETAILS = MappingProxyType({
    "SANDBOX_ENVIRONMENT_CONFIRMED": "Явно выбран тестовый контур брокера.",
    "SANDBOX_ENVIRONMENT_REQUIRED": "Для проверки необходимо явно выбрать контур sandbox.",
    "SANDBOX_TOKEN_CONFIGURED": "Отдельный токен песочницы настроен.",
    "SANDBOX_TOKEN_REQUIRED": "Нужен отдельный токен песочницы в TBANK_SANDBOX_TOKEN.",
    "SANDBOX_TOKEN_INVALID": "Формат токена песочницы не прошёл локальную проверку.",
    "SANDBOX_ACCOUNT_ID_INVALID": "Формат явно указанного счёта песочницы некорректен.",
    "SANDBOX_ACCOUNTS_READ": "Список счетов песочницы получен и проверен.",
    "SANDBOX_ACCOUNTS_EMPTY": "В ответе брокера нет счетов песочницы; нужен явный счёт.",
    "SANDBOX_ACCOUNT_SELECTION_REQUIRED": "Нужно явно выбрать счёт песочницы.",
    "SANDBOX_ACCOUNT_SELECTION_AMBIGUOUS": "Доступно несколько счетов песочницы; нужен явный выбор.",
    "SANDBOX_ACCOUNT_NOT_FOUND": "Явно указанный счёт не найден в списке счетов песочницы.",
    "SANDBOX_ACCOUNT_NOT_OPEN": "Выбранный счёт песочницы не имеет статуса открытого.",
    "SANDBOX_ACCOUNT_READ_ACCESS_REQUIRED": "Доступ на чтение выбранного счёта не подтверждён.",
    "SANDBOX_ACCOUNT_VERIFIED": "Явно выбранный счёт песочницы и доступ на чтение подтверждены.",
    "CNYRUBF_METADATA_VERIFIED": "Идентификатор CNYRUBf и параметры контракта проверены.",
    "CNYRUBF_IDENTITY_MISMATCH": "Ответ брокера не соответствует закреплённому контракту CNYRUBf.",
    "CNYRUBF_METADATA_INVALID": "Параметры контракта CNYRUBf неполны или некорректны.",
    "SANDBOX_PORTFOLIO_READ": "Портфель выбранного счёта песочницы прочитан и проверен.",
    "SANDBOX_ORDERS_READ": "Список заявок выбранного счёта песочницы прочитан и проверен.",
    "SANDBOX_RESPONSE_ACCOUNT_MISMATCH": "Ответ брокера относится к другому счёту.",
    "SANDBOX_ACCOUNTS_RESPONSE_INVALID": "Структура списка счетов песочницы некорректна.",
    "SANDBOX_PORTFOLIO_RESPONSE_INVALID": "Структура портфеля песочницы некорректна.",
    "SANDBOX_ORDERS_RESPONSE_INVALID": "Структура списка заявок песочницы некорректна.",
    "SANDBOX_URL_REJECTED": "Адрес отсутствует в списке разрешённых запросов чтения песочницы.",
    "SANDBOX_METHOD_REJECTED": "Разрешены только предусмотренные запросы чтения песочницы.",
    "SANDBOX_REDIRECT_REJECTED": "Брокер вернул перенаправление; переход не выполнялся.",
    "SANDBOX_AUTH_REJECTED": "Брокер отклонил авторизацию отдельным токеном песочницы.",
    "SANDBOX_RESOURCE_NOT_FOUND": "Запрошенный ресурс песочницы не найден.",
    "SANDBOX_RATE_LIMITED": "Брокер временно ограничил частоту запросов песочницы.",
    "SANDBOX_BROKER_ERROR": "Брокер вернул ошибку при чтении песочницы.",
    "SANDBOX_TRANSPORT_ERROR": "Не удалось завершить защищённое соединение с песочницей.",
    "SANDBOX_TIMEOUT": "Превышено время проверки ответа песочницы.",
    "SANDBOX_RESPONSE_TOO_LARGE": "Ответ песочницы превышает допустимый размер.",
    "SANDBOX_RESPONSE_ENCODING_REJECTED": "Сжатие ответа не соответствует запросу без сжатия.",
    "SANDBOX_RESPONSE_INVALID": "Ответ песочницы не является допустимым JSON-объектом.",
    "SANDBOX_TIMEOUT_INVALID": "Тайм-аут проверки должен быть от 0,1 до 10 секунд.",
    "SANDBOX_INTERNAL_ERROR": "Проверка песочницы прервана внутренней ошибкой.",
})
ACTION_CODES = frozenset({
    "SANDBOX_ENVIRONMENT_REQUIRED", "SANDBOX_TOKEN_REQUIRED", "SANDBOX_TOKEN_INVALID",
    "SANDBOX_ACCOUNT_ID_INVALID", "SANDBOX_ACCOUNTS_EMPTY",
    "SANDBOX_ACCOUNT_SELECTION_REQUIRED", "SANDBOX_ACCOUNT_SELECTION_AMBIGUOUS",
    "SANDBOX_ACCOUNT_NOT_FOUND", "SANDBOX_ACCOUNT_NOT_OPEN",
    "SANDBOX_ACCOUNT_READ_ACCESS_REQUIRED", "SANDBOX_AUTH_REJECTED",
})


class SandboxProbeError(ValueError):
    """Only fixed local diagnostics; no broker text or credential is retained."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in DETAILS else "SANDBOX_INTERNAL_ERROR"
        super().__init__(self.code)


def validate_read_url(url):
    """Exact equality rejects production, alternate hosts, ports and redirects."""
    if not isinstance(url, str) or url not in ALLOWED_URLS:
        raise SandboxProbeError("SANDBOX_URL_REJECTED")
    return url


def _timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0.1 <= value <= 10):
        raise SandboxProbeError("SANDBOX_TIMEOUT_INVALID")
    return value


@dataclass(frozen=True)
class ProbeResponse:
    status_code: int
    body: bytes = field(repr=False)


class SandboxHttpTransport:
    """Four fixed read endpoints, no retries, redirects, proxies or compression.

    The response is streamed with a byte cap and an absolute transfer deadline,
    in addition to bounded connect/read/write timeouts. A final socket read can
    consume at most one additional timeout after the transfer deadline.
    """

    def request(self, method, url, *, headers, json, timeout, follow_redirects=False):
        validate_read_url(url)
        if method != "POST" or follow_redirects is not False:
            raise SandboxProbeError("SANDBOX_METHOD_REJECTED")
        _timeout(timeout)
        try:
            import httpx

            context = ssl.create_default_context()
            root = Path(__file__).with_name("vendor") / "tbank/russian_trusted_root_ca.pem"
            if root.is_file():
                context.load_verify_locations(cafile=str(root))
            deadline = time.monotonic() + timeout
            with httpx.Client(verify=context, trust_env=False, follow_redirects=False,
                    transport=httpx.HTTPTransport(verify=context, retries=0, trust_env=False)) as client:
                with client.stream(method, url, headers=headers, json=json, timeout=timeout,
                                   follow_redirects=False) as response:
                    # Do not read an error body, which can echo headers or tokens.
                    if not 200 <= response.status_code < 300:
                        return ProbeResponse(response.status_code, b"")
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in ("", "identity"):
                        raise SandboxProbeError("SANDBOX_RESPONSE_ENCODING_REJECTED")
                    size = response.headers.get("content-length")
                    if size is not None and (not size.isdigit() or int(size) > MAX_RESPONSE_BYTES):
                        raise SandboxProbeError("SANDBOX_RESPONSE_TOO_LARGE")
                    body = bytearray()
                    for chunk in response.iter_raw():
                        if time.monotonic() >= deadline:
                            raise SandboxProbeError("SANDBOX_TIMEOUT")
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise SandboxProbeError("SANDBOX_RESPONSE_TOO_LARGE")
                        body.extend(chunk)
                    if time.monotonic() >= deadline:
                        raise SandboxProbeError("SANDBOX_TIMEOUT")
                    return ProbeResponse(response.status_code, bytes(body))
        except SandboxProbeError:
            raise
        except Exception:
            raise SandboxProbeError("SANDBOX_TRANSPORT_ERROR") from None


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError


def _request(transport, name, body, token, timeout):
    if name not in READ_ROUTES:
        raise SandboxProbeError("SANDBOX_METHOD_REJECTED")
    url = validate_read_url(SANDBOX_ROOT + PACKAGE + READ_ROUTES[name])
    try:
        response = transport.request("POST", url,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept-Encoding": "identity", "x-app-name": "7992811.VERITAS.SandboxProbe"},
            json=body, timeout=timeout, follow_redirects=False)
        status = response.status_code
        if type(status) is not int or not 100 <= status <= 599:
            raise SandboxProbeError("SANDBOX_RESPONSE_INVALID")
        if 300 <= status < 400:
            raise SandboxProbeError("SANDBOX_REDIRECT_REJECTED")
        if status in (401, 403):
            raise SandboxProbeError("SANDBOX_AUTH_REJECTED")
        if status == 404:
            raise SandboxProbeError("SANDBOX_RESOURCE_NOT_FOUND")
        if status == 429:
            raise SandboxProbeError("SANDBOX_RATE_LIMITED")
        if not 200 <= status < 300:
            raise SandboxProbeError("SANDBOX_BROKER_ERROR")
        raw = response.body
        if not isinstance(raw, bytes):
            raise SandboxProbeError("SANDBOX_RESPONSE_INVALID")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SandboxProbeError("SANDBOX_RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                                 parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise SandboxProbeError("SANDBOX_RESPONSE_INVALID") from None
        if not isinstance(payload, dict):
            raise SandboxProbeError("SANDBOX_RESPONSE_INVALID")
        if "code" in payload or "message" in payload or "error" in payload:
            raise SandboxProbeError("SANDBOX_BROKER_ERROR")
        return payload
    except SandboxProbeError:
        raise
    except Exception:
        raise SandboxProbeError("SANDBOX_TRANSPORT_ERROR") from None


def _identifier(value, code):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise SandboxProbeError(code)
    return value


def _uid(value, code):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError
        parsed = UUID(value)
        if parsed.int == 0 or str(parsed) != value.lower():
            raise ValueError
        return str(parsed)
    except (ValueError, TypeError, AttributeError):
        raise SandboxProbeError(code) from None


def _integer(value, code, *, minimum=0, maximum=INT64_MAX):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]{0,18})", value):
        number = int(value)
    else:
        raise SandboxProbeError(code)
    if not minimum <= number <= maximum:
        raise SandboxProbeError(code)
    return number


def _quotation(value, code):
    if not isinstance(value, dict):
        raise SandboxProbeError(code)
    # Protobuf JSON may omit zero-valued scalar fields.
    units = _integer(value.get("units", "0"), code, minimum=-INT64_MAX)
    nano = _integer(value.get("nano", 0), code, minimum=-999_999_999, maximum=999_999_999)
    if units * nano < 0:
        raise SandboxProbeError(code)
    return Decimal(units) + Decimal(nano) / Decimal(10**9)


def _rows(payload, field_name, code):
    rows = payload.get(field_name, [])
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(x, dict) for x in rows):
        raise SandboxProbeError(code)
    return rows


def _accounts(payload):
    code = "SANDBOX_ACCOUNTS_RESPONSE_INVALID"
    result = {}
    for row in _rows(payload, "accounts", code):
        key = _identifier(row.get("id"), code)
        if key in result or row.get("status") not in {
                "ACCOUNT_STATUS_NEW", "ACCOUNT_STATUS_OPEN", "ACCOUNT_STATUS_CLOSED",
                "ACCOUNT_STATUS_UNSPECIFIED"} or row.get("accessLevel") not in {
                "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", "ACCOUNT_ACCESS_LEVEL_READ_ONLY",
                "ACCOUNT_ACCESS_LEVEL_NO_ACCESS", "ACCOUNT_ACCESS_LEVEL_UNSPECIFIED"}:
            raise SandboxProbeError(code)
        result[key] = row
    return result


def _future(payload):
    row = payload.get("instrument")
    if not isinstance(row, dict) or row.get("uid") != CNYRUBF_UID:
        raise SandboxProbeError("CNYRUBF_IDENTITY_MISMATCH")
    code = "CNYRUBF_METADATA_INVALID"
    if (not isinstance(row.get("ticker"), str) or row["ticker"].lower() != "cnyrubf"
            or row.get("realExchange") != "REAL_EXCHANGE_MOEX"
            or not isinstance(row.get("currency"), str) or row["currency"].lower() != "rub"):
        raise SandboxProbeError(code)
    _integer(row.get("lot"), code, minimum=1, maximum=1_000_000)
    for name in ("minPriceIncrement", "minPriceIncrementAmount"):
        if _quotation(row.get(name), code) <= 0:
            raise SandboxProbeError(code)


def _scope(payload, account_id, *, required=False):
    if (required or "accountId" in payload) and payload.get("accountId") != account_id:
        raise SandboxProbeError("SANDBOX_RESPONSE_ACCOUNT_MISMATCH")


def _portfolio(payload, account_id):
    _scope(payload, account_id, required=True)
    code = "SANDBOX_PORTFOLIO_RESPONSE_INVALID"
    seen = set()
    for row in _rows(payload, "positions", code):
        _scope(row, account_id)
        uid = _uid(row.get("instrumentUid"), code)
        if uid in seen:
            raise SandboxProbeError(code)
        seen.add(uid)
        _quotation(row.get("quantity"), code)
    if "totalAmountPortfolio" in payload:
        value = payload["totalAmountPortfolio"]
        if not isinstance(value, dict) or value.get("currency") != "rub":
            raise SandboxProbeError(code)
        _quotation(value, code)


def _orders(payload, account_id):
    # GetOrdersResponse has no required account echo; the exact request scopes it.
    _scope(payload, account_id)
    code = "SANDBOX_ORDERS_RESPONSE_INVALID"
    seen = set()
    for row in _rows(payload, "orders", code):
        _scope(row, account_id)
        identifier = _identifier(row.get("orderId"), code)
        if identifier in seen:
            raise SandboxProbeError(code)
        seen.add(identifier)
        _uid(row.get("instrumentUid"), code)
        requested = _integer(row.get("lotsRequested"), code, minimum=1)
        _integer(row.get("lotsExecuted", "0"), code, maximum=requested)
        if row.get("direction") not in ("ORDER_DIRECTION_BUY", "ORDER_DIRECTION_SELL"):
            raise SandboxProbeError(code)
        if row.get("executionReportStatus") not in {
                "EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                "EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_CANCELLED",
                "EXECUTION_REPORT_STATUS_REJECTED"}:
            raise SandboxProbeError(code)


def run_probe(env=None, *, environment=None, account_id=None, transport=None, timeout=5.0, clock=None):
    """Return a redacted observation; never mutate env, an account, or the app.

    All broker I/O is opt-in through this function. Pass a mapping to isolate
    configuration in tests or a caller. Missing account selection permits only
    account discovery and stops before account-specific reads. Even one account
    requires explicit selection. The injected transport must return ProbeResponse.
    """
    source = os.environ if env is None else env
    selected_environment = environment if environment is not None else source.get("VERITAS_CURRENCY_TRADE_ENVIRONMENT")
    token = source.get("TBANK_SANDBOX_TOKEN")
    selected_account = account_id if account_id is not None else source.get("TBANK_SANDBOX_ACCOUNT_ID")
    stamp = (clock() if clock is not None else datetime.now(timezone.utc))
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise SandboxProbeError("SANDBOX_INTERNAL_ERROR")
    checked_at = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "version": VERSION, "checked_at": checked_at, "environment": "sandbox",
        "status": "FAILED", "label_ru": "Проверка чтения песочницы не завершена",
        "read_access_verified": False, "live_execution_verified": False,
        "order_submission_tested": False, "checks": [],
        "observed_config": {
            "environment_is_sandbox": selected_environment == "sandbox",
            "sandbox_token_present": isinstance(token, str) and bool(token),
            "explicit_sandbox_account_present": isinstance(selected_account, str) and bool(selected_account),
        },
    }

    def passed(code):
        result["checks"].append({"code": code, "status": "VERIFIED", "checked_at": checked_at,
                                 "detail": DETAILS[code]})

    try:
        if selected_environment != "sandbox":
            raise SandboxProbeError("SANDBOX_ENVIRONMENT_REQUIRED")
        passed("SANDBOX_ENVIRONMENT_CONFIRMED")
        if not token:
            raise SandboxProbeError("SANDBOX_TOKEN_REQUIRED")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", token):
            raise SandboxProbeError("SANDBOX_TOKEN_INVALID")
        passed("SANDBOX_TOKEN_CONFIGURED")
        if selected_account not in (None, ""):
            _identifier(selected_account, "SANDBOX_ACCOUNT_ID_INVALID")
        _timeout(timeout)
        client = transport if transport is not None else SandboxHttpTransport()
        accounts = _accounts(_request(client, "accounts", {"status": "ACCOUNT_STATUS_ALL"}, token, timeout))
        passed("SANDBOX_ACCOUNTS_READ")
        if not accounts:
            raise SandboxProbeError("SANDBOX_ACCOUNTS_EMPTY")
        if not selected_account:
            raise SandboxProbeError("SANDBOX_ACCOUNT_SELECTION_AMBIGUOUS" if len(accounts) > 1
                                    else "SANDBOX_ACCOUNT_SELECTION_REQUIRED")
        if selected_account not in accounts:
            raise SandboxProbeError("SANDBOX_ACCOUNT_NOT_FOUND")
        account = accounts[selected_account]
        if account["status"] != "ACCOUNT_STATUS_OPEN":
            raise SandboxProbeError("SANDBOX_ACCOUNT_NOT_OPEN")
        if account["accessLevel"] not in ("ACCOUNT_ACCESS_LEVEL_FULL_ACCESS", "ACCOUNT_ACCESS_LEVEL_READ_ONLY"):
            raise SandboxProbeError("SANDBOX_ACCOUNT_READ_ACCESS_REQUIRED")
        passed("SANDBOX_ACCOUNT_VERIFIED")
        _future(_request(client, "future", {"idType": "INSTRUMENT_ID_TYPE_UID", "id": CNYRUBF_UID}, token, timeout))
        passed("CNYRUBF_METADATA_VERIFIED")
        _portfolio(_request(client, "portfolio", {"accountId": selected_account, "currency": "RUB"}, token, timeout),
                   selected_account)
        passed("SANDBOX_PORTFOLIO_READ")
        _orders(_request(client, "orders", {"accountId": selected_account}, token, timeout), selected_account)
        passed("SANDBOX_ORDERS_READ")
        result.update(status="VERIFIED", label_ru="Чтение песочницы подтверждено", read_access_verified=True)
    except Exception as exc:
        code = exc.code if isinstance(exc, SandboxProbeError) else "SANDBOX_INTERNAL_ERROR"
        state = "ACTION_REQUIRED" if code in ACTION_CODES else "FAILED"
        result["checks"].append({"code": code, "status": state, "checked_at": checked_at, "detail": DETAILS[code]})
        result.update(status=state, label_ru="Требуется действие владельца" if state == "ACTION_REQUIRED"
                      else "Проверка чтения песочницы не пройдена")
    return result

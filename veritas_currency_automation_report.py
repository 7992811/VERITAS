"""Read-only, public-safe Currency diagnostics; never an execution authority.

Evidence is an observation, not a configuration change. Unknown, stale or
inconsistent evidence cannot become a successful check. Only allowlisted fields
and fixed public text leave this module; raw integration responses do not.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
import math
from pathlib import Path
import re


VERSION = 1
STATUSES = frozenset({"VERIFIED", "FAILED", "ACTION_REQUIRED", "NOT_CHECKED"})
OFFLINE_MAX_AGE_SECONDS = 24 * 60 * 60
CONNECTION_MAX_AGE_SECONDS = 15 * 60
REQUIRED_TEST_MODULES = (
    "test_veritas_offline_order_cycle",
    "test_veritas_currency_rule_consistency",
    "test_veritas_currency_console_telegram",
    "test_veritas_currency_trade_ui",
    "test_veritas_currency_sandbox_probe",
    "test_veritas_currency_automation_report",
)
SANDBOX_REQUIRED_CHECKS = frozenset({
    "SANDBOX_ENVIRONMENT_CONFIRMED", "SANDBOX_TOKEN_CONFIGURED", "SANDBOX_ACCOUNTS_READ",
    "SANDBOX_ACCOUNT_VERIFIED", "CNYRUBF_METADATA_VERIFIED", "SANDBOX_PORTFOLIO_READ", "SANDBOX_ORDERS_READ",
})
SANDBOX_PROBE_VERSION = "CURRENCY_SANDBOX_READ_PROBE_V1"
COUNT_KEYS = ("run", "passed", "failures", "errors", "skipped",
              "expected_failures", "unexpected_successes")
LABELS = {"VERIFIED": "Подтверждено", "FAILED": "Обнаружена ошибка",
          "ACTION_REQUIRED": "Нужно действие", "NOT_CHECKED": "Не подтверждено"}
TITLES = {"offline_tests": "Проверки программы", "cny_potential_filter": "Фильтр CNYRUBf",
          "sandbox_read_access": "Чтение песочницы", "sandbox_order_flow": "Тестовая заявка",
          "telegram_identity": "Бот и канал Telegram", "telegram_owner_binding": "Личная привязка"}
TEXT = {
    "OFFLINE_PASSED": "Выбранные проверки выполнены без ошибок и пропусков. Внешние подключения проверяются отдельно.",
    "OFFLINE_FAILED": "В выбранных проверках есть ошибки. Результат нельзя считать успешным.",
    "OFFLINE_INCOMPLETE": "Часть выбранных проверок пропущена или не выполнена.",
    "EVIDENCE_MISSING": "Свежего результата проверки пока нет.",
    "EVIDENCE_INVALID": "Результат проверки неполон или противоречив.",
    "EVIDENCE_STALE": "Срок действия результата истёк. Нужна повторная проверка.",
    "SOURCE_CHANGED": "Программа изменилась после проверки. Результат нужно обновить.",
    "CANONICAL_MATCH": "Порог потенциала — 1,1 × расчётных затрат. Остальные ограничения применяются отдельно.",
    "CANONICAL_MISMATCH": "Текущий канонический порог не соответствует требуемым 1,1 ×.",
    "CANONICAL_UNAVAILABLE": "Не удалось прочитать каноническое правило.",
    "SANDBOX_READ_VERIFIED": "Подтверждено чтение данных выбранного счёта в песочнице. Операции с заявками не выполнялись.",
    "SANDBOX_READ_FAILED": "Проверка чтения песочницы не прошла.",
    "SANDBOX_SCOPE_INVALID": "Результат не подтверждает проверку только песочницы.",
    "SANDBOX_TOKEN_REQUIRED": "В проверенной конфигурации отсутствует доступ к песочнице.",
    "SANDBOX_ACCOUNT_REQUIRED": "В проверенной конфигурации не выбран счёт песочницы.",
    "SANDBOX_MODE_REQUIRED": "Для этой проверки нужно выбрать режим песочницы.",
    "READ_ONLY_SCOPE": "Эта диагностика проверяет чтение данных. Отправка и исполнение тестовой заявки здесь не подтверждаются.",
    "TELEGRAM_VERIFIED": "Подтверждены бот, канал и право публикации. Доставка сообщения отдельно не проверялась.",
    "TELEGRAM_OWNER_VERIFIED": "Личная привязка владельца к выбранному боту подтверждена.",
    "TELEGRAM_IDENTITY_FAILED": "Проверенный бот или канал не соответствует выбранному подключению.",
    "TELEGRAM_CHECK_FAILED": "Проверка подключения Telegram не прошла.",
    "TELEGRAM_POST_PERMISSION_REQUIRED": "Боту требуется право публикации в выбранном канале.",
    "TELEGRAM_OWNER_BINDING_REQUIRED": "Нужна личная привязка владельца через выбранного бота.",
}
ACTION_TEXT = {
    "SANDBOX_TOKEN_REQUIRED": "Добавить доступ к песочнице в закрытые настройки сервиса.",
    "SANDBOX_ACCOUNT_REQUIRED": "Выбрать счёт песочницы в закрытых настройках сервиса.",
    "TELEGRAM_POST_PERMISSION_REQUIRED": "Предоставить @AxednewsI_bot право публикации в @axednewz.",
    "TELEGRAM_OWNER_BINDING_REQUIRED": "Завершить личную привязку через @AxednewsI_bot.",
}


def _time(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value):
    stamp = _time(value)
    return stamp.isoformat().replace("+00:00", "Z") if stamp else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _enum(value, allowed, fallback):
    return value if isinstance(value, str) and value in allowed else fallback


def _fresh(evidence, now, max_age):
    if not evidence:
        return "EVIDENCE_MISSING"
    stamp = _time(evidence.get("checked_at"))
    if stamp is None or (stamp - now).total_seconds() > 30:
        return "EVIDENCE_INVALID"
    if (now - stamp).total_seconds() >= max_age:
        return "EVIDENCE_STALE"
    return None


def _check(code, status, reason, *, checked_at=None, max_age=None, **values):
    stamp = _time(checked_at)
    result = {"code": code, "status": status, "reason": reason,
              "checked_at": _iso(stamp), "valid_until": _iso(stamp + timedelta(seconds=max_age)) if stamp and max_age else None}
    result.update(values)
    return result


def source_fingerprint(root=None):
    """Bind local evidence to Python sources, without reading credentials."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    paths = sorted(set(root.glob("*.py")) | set((root / "tools").glob("*.py")))
    digest = sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _counts(raw):
    raw = _mapping(raw)
    if not all(key in raw for key in COUNT_KEYS):
        return None
    result = {key: raw[key] for key in COUNT_KEYS}
    if any(type(value) is not int or not 0 <= value <= 1000000 for value in result.values()):
        return None
    if result["run"] <= 0 or sum(result[key] for key in COUNT_KEYS if key != "run") != result["run"]:
        return None
    return result


def _offline(evidence, now, fingerprint):
    evidence = _mapping(evidence)
    counts = _counts(evidence.get("counts"))
    values = {"counts": counts, "source": _enum(evidence.get("source"), {"local_unittest", "github_actions"}, "unspecified")}
    kwargs = dict(checked_at=evidence.get("checked_at"), max_age=OFFLINE_MAX_AGE_SECONDS, **values)
    reason = _fresh(evidence, now, OFFLINE_MAX_AGE_SECONDS)
    if reason:
        return _check("offline_tests", "NOT_CHECKED", reason, **kwargs)
    if not counts or _enum(evidence.get("status"), STATUSES, None) is None or values["source"] == "unspecified":
        return _check("offline_tests", "NOT_CHECKED", "EVIDENCE_INVALID", **kwargs)
    if evidence.get("source_fingerprint") != fingerprint or not re.fullmatch(r"[a-f0-9]{64}", str(fingerprint)):
        return _check("offline_tests", "NOT_CHECKED", "SOURCE_CHANGED", **kwargs)
    if counts["failures"] or counts["errors"] or counts["unexpected_successes"] or evidence.get("status") == "FAILED":
        return _check("offline_tests", "FAILED", "OFFLINE_FAILED", **kwargs)
    modules = evidence.get("modules")
    if (not isinstance(modules, list) or any(not isinstance(item, str) for item in modules)
            or not set(REQUIRED_TEST_MODULES).issubset(modules) or counts["skipped"] or counts["expected_failures"]):
        return _check("offline_tests", "ACTION_REQUIRED", "OFFLINE_INCOMPLETE", **kwargs)
    if evidence.get("status") != "VERIFIED":
        return _check("offline_tests", "NOT_CHECKED", "EVIDENCE_INVALID", **kwargs)
    return _check("offline_tests", "VERIFIED", "OFFLINE_PASSED", **kwargs)


def _config(raw, now):
    raw = _mapping(raw)
    if _fresh(raw, now, CONNECTION_MAX_AGE_SECONDS):
        return {}
    return {"checked_at": _iso(raw.get("checked_at")), **{
        key: raw[key] for key in ("sandbox_token_present", "explicit_sandbox_account_present", "environment_is_sandbox")
        if type(raw.get(key)) is bool}}


def _sandbox(evidence, config, now):
    evidence = _mapping(evidence)
    kwargs = dict(checked_at=evidence.get("checked_at"), max_age=CONNECTION_MAX_AGE_SECONDS)
    reason = _fresh(evidence, now, CONNECTION_MAX_AGE_SECONDS)
    if reason:
        status, why = "NOT_CHECKED", reason
    elif evidence.get("environment") != "sandbox" or evidence.get("live_execution_verified") is not False or evidence.get("order_submission_tested") is not False:
        status, why = "FAILED", "SANDBOX_SCOPE_INVALID"
    elif evidence.get("status") == "FAILED":
        status, why = "FAILED", "SANDBOX_READ_FAILED"
    elif evidence.get("status") == "VERIFIED":
        checks = evidence.get("checks")
        verified = (evidence.get("version") == SANDBOX_PROBE_VERSION
                    and evidence.get("read_access_verified") is True and isinstance(checks, list) and bool(checks)
                    and all(isinstance(item, dict) and item.get("status") == "VERIFIED"
                            and isinstance(item.get("code"), str) and not _fresh(item, now, CONNECTION_MAX_AGE_SECONDS) for item in checks)
                    and len(checks) == len(SANDBOX_REQUIRED_CHECKS)
                    and {item["code"] for item in checks} == SANDBOX_REQUIRED_CHECKS
                    and all(config.get(key) is True for key in ("sandbox_token_present", "explicit_sandbox_account_present", "environment_is_sandbox")))
        status, why = ("VERIFIED", "SANDBOX_READ_VERIFIED") if verified else ("NOT_CHECKED", "EVIDENCE_INVALID")
    else:
        status, why = "NOT_CHECKED", "EVIDENCE_MISSING"
    if status != "FAILED":
        for key, missing in (("environment_is_sandbox", "SANDBOX_MODE_REQUIRED"),
                             ("sandbox_token_present", "SANDBOX_TOKEN_REQUIRED"),
                             ("explicit_sandbox_account_present", "SANDBOX_ACCOUNT_REQUIRED")):
            if config.get(key) is False:
                status, why = "ACTION_REQUIRED", missing
                kwargs["checked_at"] = config.get("checked_at")
                break
    return _check("sandbox_read_access", status, why, **kwargs)


def _telegram(evidence, now):
    evidence = _mapping(evidence)
    kwargs = dict(checked_at=evidence.get("checked_at"), max_age=CONNECTION_MAX_AGE_SECONDS)
    reason = _fresh(evidence, now, CONNECTION_MAX_AGE_SECONDS)
    if reason:
        return _check("telegram_identity", "NOT_CHECKED", reason, **kwargs)
    if evidence.get("bot_identity_verified") is False or evidence.get("channel_identity_verified") is False:
        return _check("telegram_identity", "FAILED", "TELEGRAM_IDENTITY_FAILED", **kwargs)
    if evidence.get("status") == "FAILED":
        return _check("telegram_identity", "FAILED", "TELEGRAM_CHECK_FAILED", **kwargs)
    if evidence.get("can_post_messages") is False:
        return _check("telegram_identity", "ACTION_REQUIRED", "TELEGRAM_POST_PERMISSION_REQUIRED", **kwargs)
    if evidence.get("status") == "VERIFIED" and all(evidence.get(key) is True for key in (
            "bot_identity_verified", "channel_identity_verified", "can_post_messages")):
        return _check("telegram_identity", "VERIFIED", "TELEGRAM_VERIFIED", **kwargs)
    return _check("telegram_identity", "NOT_CHECKED", "EVIDENCE_INVALID", **kwargs)


def _owner_binding(evidence, now):
    evidence = _mapping(evidence)
    kwargs = dict(checked_at=evidence.get("checked_at"), max_age=CONNECTION_MAX_AGE_SECONDS)
    reason = _fresh(evidence, now, CONNECTION_MAX_AGE_SECONDS)
    if reason:
        return _check("telegram_owner_binding", "NOT_CHECKED", reason, **kwargs)
    if evidence.get("bot_identity_verified") is not True:
        return _check("telegram_owner_binding", "NOT_CHECKED", "EVIDENCE_INVALID", **kwargs)
    if evidence.get("owner_binding_verified") is False:
        return _check("telegram_owner_binding", "ACTION_REQUIRED", "TELEGRAM_OWNER_BINDING_REQUIRED", **kwargs)
    if evidence.get("owner_binding_verified") is True:
        return _check("telegram_owner_binding", "VERIFIED", "TELEGRAM_OWNER_VERIFIED", **kwargs)
    return _check("telegram_owner_binding", "NOT_CHECKED", "EVIDENCE_MISSING", **kwargs)


def _canonical(now):
    try:
        from veritas_costs import policy
        multiple = policy("CNYRUBF")["entry_cost_multiple"]
        good = type(multiple) in (int, float) and math.isfinite(multiple) and math.isclose(multiple, 1.1, rel_tol=0, abs_tol=1e-12)
        return _check("cny_potential_filter", "VERIFIED" if good else "FAILED", "CANONICAL_MATCH" if good else "CANONICAL_MISMATCH",
                      checked_at=now, max_age=OFFLINE_MAX_AGE_SECONDS, expected_multiple=1.1,
                      observed_multiple=multiple if type(multiple) in (int, float) and math.isfinite(multiple) else None)
    except (ImportError, KeyError, TypeError, ValueError):
        return _check("cny_potential_filter", "NOT_CHECKED", "CANONICAL_UNAVAILABLE", checked_at=now)


def build_report(*, offline_evidence=None, sandbox_report=None, config_observation=None,
                 telegram_observation=None, now=None, expected_source_fingerprint=None):
    """Build a sanitized observation. No network, database, message or order I/O.

    Telegram booleans must describe the pinned @AxednewsI_bot / @axednewz scope.
    Absence of an observation is unknown, never proof of absent credentials.
    """
    now = _time(now if now is not None else datetime.now(timezone.utc))
    if now is None:
        raise ValueError("A timezone-aware observation time is required")
    fingerprint = expected_source_fingerprint if expected_source_fingerprint is not None else source_fingerprint()
    sandbox_report = _mapping(sandbox_report)
    observed = {**_mapping(sandbox_report.get("observed_config")), "checked_at": sandbox_report.get("checked_at")}
    config = _config(config_observation, now) if config_observation is not None else _config(observed, now)
    checks = [_offline(offline_evidence, now, fingerprint), _canonical(now),
              _sandbox(sandbox_report, config, now),
              _check("sandbox_order_flow", "NOT_CHECKED", "READ_ONLY_SCOPE"),
              _telegram(telegram_observation, now), _owner_binding(telegram_observation, now)]
    status = next((state for state in ("FAILED", "ACTION_REQUIRED", "NOT_CHECKED")
                   if any(check["status"] == state for check in checks)), "VERIFIED")
    actions = [check["reason"] for check in checks if check["status"] == "ACTION_REQUIRED" and check["reason"] in ACTION_TEXT]
    return {"version": VERSION, "generated_at": _iso(now), "scope": "INFORMATIONAL_DIAGNOSTICS", "status": status,
            "execution_permission_granted": False, "real_execution_readiness": "NOT_CHECKED",
            "sandbox_read_access_verified": checks[2]["status"] == "VERIFIED",
            "source_fingerprint": fingerprint if isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint) else None,
            "public_destinations": {"bot": "@AxednewsI_bot", "channel": "@axednewz"},
            "checks": checks, "owner_action_codes": list(dict.fromkeys(actions))}


def render_html(report):
    """Render only allowlisted public fields, even if handed a foreign report."""
    report = _mapping(report)
    cards = []
    for raw in report.get("checks", []) if isinstance(report.get("checks"), list) else []:
        row = _mapping(raw)
        code = row.get("code")
        if not isinstance(code, str) or code not in TITLES:
            continue
        status = _enum(row.get("status"), STATUSES, "NOT_CHECKED")
        reason = _enum(row.get("reason"), TEXT, "EVIDENCE_INVALID")
        checked, valid_until = _iso(row.get("checked_at")), _iso(row.get("valid_until"))
        detail = ""
        counts = _counts(row.get("counts")) if code == "offline_tests" else None
        if counts:
            detail = f'<p class="counts">Выполнено: {counts["run"]} · Успешно: {counts["passed"]} · Пропущено: {counts["skipped"]} · Ошибок: {counts["failures"] + counts["errors"]}</p>'
        stamp = f'<p class="timestamp">Проверено: <time datetime="{checked}">{checked}</time>' if checked else ""
        if stamp:
            stamp += f'<br>Действует до: <time datetime="{valid_until}">{valid_until}</time></p>' if valid_until else "</p>"
        cards.append(f'<section class="card" data-status="{status}" data-valid-until="{valid_until or ""}"><div class="card-heading"><h2>{TITLES[code]}</h2><span class="badge {status}">{LABELS[status]}</span></div><p class="detail">{TEXT[reason]}</p>{detail}{stamp}</section>')
    action_codes = report.get("owner_action_codes")
    actions = [ACTION_TEXT[code] for code in action_codes if isinstance(code, str) and code in ACTION_TEXT] if isinstance(action_codes, list) else []
    action_html = "<ul>" + "".join(f"<li>{text}</li>" for text in dict.fromkeys(actions)) + "</ul>" if actions else "<p>Действия владельца по текущим данным не установлены. Непроверенные подключения ещё требуют автоматической диагностики.</p>"
    generated = _iso(report.get("generated_at"))
    return '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer"><meta name="robots" content="noindex,nofollow"><meta http-equiv="refresh" content="60"><title>VERITAS · Проверка валютного портфеля</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#182b3a;background:#f1f5f7}*{box-sizing:border-box}body{margin:0}main{max-width:1040px;margin:auto;padding:40px 24px 64px}header{margin-bottom:28px}.eyebrow{font-size:13px;letter-spacing:.12em;color:#547080;margin:0 0 12px}h1{font-size:clamp(27px,5vw,40px);letter-spacing:-.03em;line-height:1.15;margin:0 0 15px}header p{max-width:760px;font-size:17px;line-height:1.6;color:#415866}.scope{background:#e3ebf0;border-left:4px solid #45677d;padding:16px 18px;border-radius:5px;margin:24px 0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{background:white;border:1px solid #d9e2e7;border-radius:12px;padding:22px;min-width:0}.card-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.card h2{font-size:19px;margin:0;line-height:1.3}.badge{font-size:12px;line-height:1.3;border-radius:6px;padding:5px 8px;font-weight:600;white-space:nowrap}.VERIFIED{background:#e2f2eb;color:#20654b}.FAILED{background:#fae7e6;color:#9e302b}.ACTION_REQUIRED{background:#fff0cc;color:#795517}.NOT_CHECKED{background:#edf0f3;color:#546575}.card p{font-size:15px;line-height:1.6;margin:17px 0 0}.card .timestamp{font-size:12px;color:#6b7c87;overflow-wrap:anywhere}.card .counts{font-size:13px}.next{margin-top:24px;padding:24px;background:#e8eef2;border-radius:12px}.next h2{font-size:20px;margin:0 0 14px}.next p,.next li{font-size:15px;line-height:1.7}.next ul{padding-left:20px;margin:0}footer{margin-top:25px;color:#6b7c87;font-size:12px;line-height:1.7}a{color:#245d7d;text-underline-offset:3px;display:inline-block;min-height:44px;padding:10px 0}time{font-variant-numeric:tabular-nums}@media(max-width:720px){main{padding:26px 16px 40px}.grid{grid-template-columns:1fr}.card{padding:19px}.card-heading{flex-wrap:wrap}.badge{white-space:normal}}
</style></head><body><main><header><p class="eyebrow">VERITAS / CURRENCY</p><h1>Проверка валютного портфеля</h1><p>Фактические результаты проверок программы, песочницы и Telegram. Подтверждение появляется только при наличии подходящего свежего результата.</p></header><div class="scope">Это страница диагностики. Она не подключает реальный счёт к исполнению, не подтверждает сделки и не отправляет заявки.</div><div class="grid">''' + "".join(cards) + '''</div><section class="next"><h2>Что потребуется от владельца</h2>''' + action_html + '''</section><footer>Личный бот: <a href="https://t.me/AxednewsI_bot" rel="noreferrer">@AxednewsI_bot</a> · Канал: <a href="https://t.me/axednewz" rel="noreferrer">@axednewz</a><br>Время снимка: ''' + escape(generated or "не указано") + '''. Все отметки времени — UTC. Состояние реального исполнения этой страницей не проверяется.</footer></main>
</body></html>'''

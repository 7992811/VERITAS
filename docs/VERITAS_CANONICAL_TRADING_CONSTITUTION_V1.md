# VERITAS Canonical Trading Constitution v1

Дата фиксации: 06.10.2026  
База реализации: `main@4c2d005` / `R85_FINAL_AUTHORITY_LOCK`  
Статус: **каноническая политика paper-runtime, внедрена в main**. Merge commit: `bf02964a10d37d7d119d0de4808aadb31965963b`. Сам файл не переключает торговое исполнение; рабочий R85 остаётся единственным runtime-authority до отдельного интеграционного изменения.

## 1. Зачем это нужно

VERITAS прошёл через много последовательных Rxx-слоёв. Часть старых функций остаётся в коде ради обратной совместимости и аудита, хотя R85 уже жёстко привязал исполнение к финальным callable-функциям. Эта конституция определяет одно смысловое правило для каждой стадии решения и не позволяет старой обёртке незаметно изменить торговую политику.

Главный принцип:

**одно торговое решение → один канонический путь политики.**

Порядок стадий неизменен:

`DATA → THESIS → TIMING → ECONOMICS → RISK → SIZE → LIFECYCLE → LEARNING`.

Поздняя стадия не имеет права отменить hard-veto более ранней стадии.

## 2. Целевая функция

1. Жёсткое условие: целевая экономика сделки после расходов должна быть положительной.
2. Среди допустимых сделок приоритет №1 — устойчивый win rate, цель ≥65%.
3. Приоритет №2 — устойчивая чистая прибыль и захват крупных движений.
4. Приоритет №3 — контроль просадки.

Таким образом устранён старый конфликт R35/R40: **положительное ожидание после расходов — не конкурирующая цель, а обязательное условие допуска; дальше оптимизируется win rate и прибыльность.**

## 3. Hard veto

Новый риск запрещён только по канонической жёсткой причине:

- неподдерживаемый актив/источник;
- primary source gate failed;
- рынок закрыт для нового риска;
- stale execution quote;
- source identity mismatch;
- exact contract mismatch;
- NQ без прямой futures-котировки;
- неверная цена;
- отсутствующий/неправильно направленный stop;
- отсутствующая/неправильно направленная target geometry;
- explicit hard thesis invalidation;
- подтверждённый конфликт execution-TF;
- 5m контртренд против senior без достаточного подтверждения;
- фактическая цена исполнения уже слишком далеко от текущего trigger (anti-chase);
- подтверждённый отрицательный edge сетапа;
- цель убыточна после расходов;
- ожидаемое движение меньше канонического cost buffer;
- превышен stop-risk;
- hard drawdown stop;
- повторное использование того же event без нового подтверждения.

## 4. Soft veto / уменьшение размера

Следующие факторы **не должны сами по себе оставлять опубликованный текущий LONG/SHORT без позиции**, если hard-veto отсутствует:

- R/R ниже стандартного floor, но net economics положительна;
- net R/R ниже стандартного floor, но цель остаётся прибыльной;
- старый parent event устарел;
- старая цель parent event уже достигнута;
- старый WAIT_RETEST;
- extension старого parent event;
- маленькая статистическая выборка;
- слабая калибровка;
- senior-context caution без execution-TF конфликта;
- маргинальная история сетапа;
- отрицательная история, вызванная ошибками управления, а не направления;
- soft lower-TF invalidation стратегической 3d/7d позиции.

Реакция: уменьшить размер, оставить probe, прекратить добор или управлять tactical sleeve.

## 5. Сигнал

Качество проверяется **до** публикации сигнала.

После того как VERITAS опубликовал текущий `LONG` или `SHORT`, сигнал считается authority для начала staged risk. Старые parent-состояния не могут повторно превратить его в cash.

Исключение — hard-veto из раздела 3.

Правило R85 сохраняется: сигнал выбирает направление, но **свежая same-source цена исполнения** определяет реальную цену, anti-chase, stop geometry и финальную экономику.

## 6. Издержки

Канонические параметры:

| Параметр | Значение |
|---|---:|
| Комиссия | 0,04% за сторону |
| Модельное проскальзывание | 0,04% за сторону |
| Базовый round trip | 0,16% |
| Запас к расходам | 1,1× |
| Минимальный потенциал | max(0,19%; 1,1 × моделируемые расходы) |
| Фондирование | 16% годовых |
| Первые 24 часа | без фондирования |
| База | ACT/365.25 |

При базовых 0,16% расходов формула даёт минимальный ход 0,19%. Поэтому 0,19% — абсолютный floor, а не итоговый threshold при любых расходах.

Cost-negative probe запрещён.

## 7. Источники и контракты

- Для paper достаточно одного валидного primary source.
- Позиция получает immutable source identity на входе.
- Entry, mark, MFE/MAE, stop, TP и exit используют один source identity.
- При наличии идентификатора контракта он фиксируется на весь lifecycle.
- Другой источник не может закрыть существующую позицию.
- Если pinned source пропал или устарел — mark/execution замораживаются, используется последняя source-verified цена.
- Смешанные источники и contract mismatch исключаются из обучения.
- Ledger не переписывается задним числом.
- Для NQ proxy/QQQ/cash-index не может быть execution quote.

## 8. Рыночная структура

- Пробой и исходный уровень строятся только по уже наблюдённой/закрытой структуре.
- Уровень и event ID не перемещаются вслед за ценой.
- LONG continuation: HH/HL.
- SHORT continuation: LH/LL.
- Breakout подтверждается сочетанием break + acceptance + activity/volume + volatility + subsequent structure.
- В RANGE_LOW_VOL требования к подтверждению повышаются.
- Retest/hold — самостоятельный тип входа.
- Новый continuation event получает новый ID и новую точку отсчёта lateness.

## 9. Мультитаймфрейм

- 1m/5m — execution timing и ранняя локальная структура.
- 1h/4h — управление рабочим движением.
- 1d/3d/7d — senior context и core thesis.
- Senior TF может уменьшить размер fast setup, но не является автоматическим veto.
- Младший soft-конфликт не имеет права полностью закрыть intact senior core.
- Hard risk и explicit hard invalidation остаются немедленными на любом горизонте.

## 10. Anti-chase

Anti-chase считается по **фактической свежей цене исполнения**, а не по старой сигнальной цене.

Для текущего event сравниваются:
- executable price;
- trigger;
- realized volatility;
- уже consumed move.

Новый continuation event начинает собственный anti-chase clock. Нельзя считать его поздним только потому, что первоначальный parent breakout произошёл давно.

## 11. Портфели

### Impulse
- normal signal: 20%;
- SUPER: 40%;
- max asset/gross: 50%;
- hard DD: 15%.

### Aggressive
- normal signal: 50%;
- SUPER: 100%;
- dynamic scale: 75% → 100% → 125% → 150% → 200% → 300% → 400% → максимум 500%;
- leverage разрешается только после усиления структуры/подтверждений и при ограниченном stop-risk;
- hard DD: 20%.

### Champion
- normal: 10%;
- SUPER: 25%;
- canonical single-asset cap: 100%;
- hard DD: 15%.

### Challenger
- normal: 10%;
- SUPER: 25%;
- canonical single-asset cap: 100%;
- hard DD: 15%.

### Валютный портфель
Канонизированы ранее заданные владельцем параметры:
- капитал 10 000 ₽;
- только CNYRUBF;
- LONG / SHORT / CASH;
- максимум 10× gross;
- максимальная просадка 35%;
- перенос через выходные разрешён;
- шаг размера 5%.

Отдельный per-trade stop-risk override не придумывается: используется структурный risk governor. Портфель настроен как paper-book: 10 000 ₽, CNYRUBF, до 10× gross, hard DD 35%; live-торговля остаётся отдельно fail-closed.

## 12. Риск

Для модельных основных портфелей:
- структурный stop-risk одной идеи ≤2% NAV;
- drawdown уменьшает размер и gross, а не качество сигнала;
- Standard: caution с 8%, defense с 11%, усиленная defense с 13,5%, hard stop 15%;
- Aggressive: caution с 10%, defense с 14%, усиленная defense с 17%, hard stop 20%.

Для будущего реального счёта действует отдельный строгий fail-closed профиль:
- 0,5% stop-risk одной идеи;
- 2,5% total open stop-risk;
- 1,25% correlated stop-risk;
- 25% одного актива;
- 1,25× max gross;
- daily loss stop 2%;
- weekly loss stop 5%;
- hard DD 10%.

Research leverage нельзя автоматически переносить на live.

## 13. Размер

- Шаг позиции — 5%.
- Размер определяется после stop geometry.
- Нельзя менять структуру stop под желаемый размер.
- Aggressive leverage зарабатывается подтверждением.
- B setup — только небольшой exploratory risk в Impulse/Aggressive.
- C setup — NO_TRADE.
- Champion/Challenger не получают неявное плечо без отдельного разрешения.

## 14. Добор

Добор разрешается только если одновременно:
- позиция движется в нужную сторону;
- появился отдельный новый confirmation event;
- есть новый structural anchor или подтверждённый breakout;
- остаётся достаточный потенциал;
- post-cost economics нового add положительна;
- общий stop-risk остаётся в лимите.

Запрещено:
- автоматическое усреднение проигрывающей позиции;
- восстановление ранее зафиксированного объёма без нового setup;
- расширение protected stop после reload.

## 15. Stop / protection

- Сначала определяется structural invalidation.
- LONG stop — ниже подтверждённого swing low/support.
- SHORT stop — выше swing high/resistance.
- Используется volatility-aware buffer.
- Лучше уменьшить size, чем ставить stop внутри нормального шума.
- Защитный stop ratchet only: LONG только вверх, SHORT только вниз.
- Reload не ослабляет уже защищённый stop.

## 16. Breakeven

Breakeven — экономический, а не номинальный.

Перевод защиты к BE разрешён только когда движение покрывает:
- уже уплаченные комиссии;
- ожидаемый exit cost;
- проскальзывание;
- применимое фондирование;
- safety buffer.

Нельзя переводить stop к entry после первого маленького положительного тика.

## 17. Прибыль и выход

Lifecycle:

`OPEN → ADD → PROTECT → HARVEST → RUNNER → EXIT`.

- Сильный тренд: меньшая частичная фиксация, больший runner.
- Слабый momentum/близкая senior-цель: большая фиксация.
- После harvest reload — новая add-сделка, а не автоматическое восстановление.
- Микро opposite signal внутри комиссии не является причиной churn.
- Soft INVALIDATED/NO_TRADE/generic WAIT не закрывает позицию в fee-negative результат, пока thesis и hard risk целы.
- Полный немедленный exit: true stop/risk breach, explicit hard thesis invalidation, подтверждённый structural/direction failure или portfolio hard stop.

## 18. Обучение

Один market episode = одно независимое наблюдение, даже если его исполнили несколько портфелей.

Отдельные labels:
- wrong direction;
- false entry;
- late entry;
- right direction / bad entry;
- stop too tight;
- stop too wide;
- early exit;
- missed trend;
- missed re-entry;
- bad size;
- source/data error.

Management-dominated loss не должен автоматически снижать оценку направления.

Из обучения исключаются:
- mixed sources;
- contract mismatch;
- administrative rebase;
- повреждённая цена;
- отсутствие надёжного MFE/MAE path;
- дубли одного рыночного эпизода.

Новые правила начинаются в SHADOW и требуют OOS + Vault + cost stress + time/regime stability + достаточную выборку.

## 19. 60 канонических правил

Машинно-читаемый полный реестр: `veritas_canonical_constitution.py::CANONICAL_RULES`.

Группы:
- CTC01–08: governance/objective/classification;
- CTC09–15: data/source identity;
- CTC16–24: signal/admission;
- CTC25–32: structure/multitimeframe/timing;
- CTC33–37: economics;
- CTC38–45: risk/sizing;
- CTC46–55: lifecycle/add/stop/profit/exit;
- CTC56–60: learning.

## 20. Статус устранения расхождений

GAP01–GAP10 закрыты в интеграционной ветке:

- Champion и Challenger: single-asset cap 100%.
- Currency: CONFIGURED, 10 000 ₽, только CNYRUBF, 10× max gross, DD 35%, weekend carry.
- Уточнение владельца от 08.10.2026: для брокерских заявок Currency минимальное отношение дохода к риску после расчётных издержек — **1,0015**. Значение едино для ручных и модельных заявок, подготовки, повторной проверки и допуска всего счёта. Источник: `PORTFOLIO_POLICIES["Currency"]["live_minimum_net_reward_risk"]`. Общий порог `VERITAS_FINAL_MIN_RR` его не заменяет. Лимит риска на сделку, проверка издержек и отдельное подтверждение заявки сохраняются.
- Admission: единый CanonicalAdmissionEngine.
- R35/R40: единая целевая функция из CTC.
- Расходы: 0,04% комиссия + 0,04% slippage за сторону, 1,1× buffer.
- Knowledge automation: discovery работает и на web-role; LLM-компиляция shadow-гипотез включается при наличии API key.
- Circular import final-authority устранён.
- Zero-exposure fast-memory API сохраняет `api_source=live_memory` и добавляет Currency к core books.

## 21. Что уже исправлено R81–R85 и не является открытым конфликтом

- R81: комиссия восстановлена до 0,04%, cost buffer до 1,1×.
- R81: cost-negative probe запрещён.
- R81: soft INVALIDATED/NO_TRADE больше не является hard-exit открытой позиции.
- R83: направление сигнала отделено от свежей execution quote; anti-chase считается по фактической цене.
- R83: market closed отличается от broken source при защитном monitoring.
- R85: финальная runtime authority закреплена явно; исторические определения не могут молча вернуть себе исполнение.

## 22. Следующий этап внедрения

Не добавлять новый Rxx поверх старого.

Следующий интеграционный этап должен:
1. связать runtime trace с CTC hard/soft reason codes;
2. исправить GAP01–GAP03;
3. свести admission к одной функции без изменения уже проверенной R85 семантики;
4. свести size management к одному lifecycle authority;
5. запускать regression tests против этой конституции;
6. после этого архивировать старые Rxx как implementation history.

После прохождения регрессий CTC v1 становится нормативным и исполнительным источником правил для paper runtime; live capital остаётся независимо fail-closed.

# VERITAS v72 — Market Intelligence & Causal Decision Engine

Дата пакета: 22.09.2026

## Что загружать

### ЗАМЕНИТЬ
- `veritas_v70.py` → файлом из этого пакета с тем же именем.

### ДОБАВИТЬ
- `test_veritas_v72.py`
- `README_V72.md`

`veritas_intelligence.py` в этом апдейте НЕ заменяется: текущий production-файл (~960 КБ) уже содержит v70.8.4 range-participation, CNYRUBF, многоязычный research и последние исправления. v72 подключается через существующий импорт `import veritas_v70 as V70`.

## Пороговая политика v72

Согласованные ранее изменения:
- Trend onset: `0.60 → 0.50`
- Trend day: `0.68 → 0.60`
- Mature impulse: `0.74` — сохраняем строгим
- Tactical BUY/SELL: ожидаемый ход не менее `0.4%`

Дополнительное расширение участия в v72:
- `MIN_DIRECTIONAL_SCORE: 0.20 → 0.15`
- `expected_move / stop: 1.00 → 0.75`
- ранний вход: `35%`
- подтверждённый вход: `70%`
- сильный подтверждённый импульс: до `100%`
- alert confidence: `0.30 → 0.25`

Если соответствующая переменная уже вручную задана в Render Environment, она имеет приоритет над этими default-значениями.

## Новые качественные слои

- Market State Engine 2.0
- Expectation Gap Engine 2.0
- Positioning Intelligence 2.0
- Liquidity Map
- Global Causal Graph
- Contradiction Engine
- Red Team Engine
- Thesis Decay
- Dynamic Invalidation
- Path Forecast templates
- Data Trust
- Source Reliability learning gate
- Narrative deduplication
- Shock Classification
- Adaptive Horizon Selection
- Portfolio Brain
- более агрессивный staged sizing
- разделение ENTRY_VETO / THESIS_VETO / DATA_VETO

## Совместимость

Текущий `veritas_intelligence.py` обращается к модулю V70 только через:
- `V70.investor_asset_view(...)`
- `V70.quality_board(...)`
- `V70.pretrade_gate(...)`

Все три интерфейса сохранены.

## Проверка после загрузки

Локально/в CI:
```bash
python -m py_compile veritas_v70.py
python test_veritas_v72.py
```

После Render deploy проверить:
- `/api/v1/ping` — версия должна содержать `v72.0`
- `/api/v1/v70` или текущий endpoint quality board — статус `RELEASE_CANDIDATE_V72`
- `/api/v1/settings` — проверить фактический `min_directional_score`
- dashboard — рост количества directional-кандидатов без роста DATA_VETO
- `NO_TRADE` / missed opportunity — должен начать снижаться по мере накопления эпизодов

## Важное ограничение

v72 не выдумывает отсутствующие данные. Positioning, liquidity, source reliability и часть causal-слоёв переходят в `DATA_REQUIRED/BUILDING`, если host не передал соответствующие наблюдения.

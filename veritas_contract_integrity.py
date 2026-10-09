"""Contract/source integrity prepass extracted from the legacy portfolio monolith.

This module owns no portfolio authority. It only validates that an existing
position is being marked/executed against the same contract/source identity and
returns a filtered candidate/price view to the caller.
"""
import json
import veritas_price_source as VPS


def entry_patch(base_patch, row):
    d = dict(base_patch or {})
    row = row or {}
    contract = row.get("contract") or {}
    src = row.get("source_names") or {}
    d["contract_identity"] = {
        "asset": row.get("asset"),
        "contract_id": contract.get("secid") or contract.get("symbol") or row.get("contract_id"),
        "price_unit": contract.get("price_unit"),
        "primary_source": src.get("primary"),
        "verification_mode": row.get("verification_mode"),
        "continuous_series": bool(contract.get("continuous") or row.get("continuous_series")),
    }
    return d


def same_contract(payload, row):
    if (payload or {}).get("price_source_lock"):
        lock = payload["price_source_lock"]
        return VPS.same(lock, VPS.identity(lock.get("asset"), row))
    p = (payload or {}).get("contract_identity") or {}
    r = (row or {}).get("contract") or {}
    rid = r.get("secid") or r.get("symbol") or (row or {}).get("contract_id")
    pid = p.get("contract_id")
    if pid and rid:
        return str(pid) == str(rid)
    psrc = p.get("primary_source")
    rsrc = ((row or {}).get("source_names") or {}).get("primary")
    pmode = p.get("verification_mode")
    rmode = (row or {}).get("verification_mode")
    return bool(
        (not psrc or not rsrc or str(psrc) == str(rsrc))
        and (not pmode or not rmode or str(pmode) == str(rmode))
    )


def cross_source_disagreement(row):
    row = row or {}
    try:
        primary = float(row.get("price") or 0.0)
        secondary = float(row.get("secondary_price") or row.get("coinbase_price") or 0.0)
    except Exception:
        return None
    if primary <= 0 or secondary <= 0:
        return None
    divergence = abs(primary - secondary) / max(1e-9, (primary + secondary) / 2.0)
    return {"primary": primary, "secondary": secondary, "divergence": divergence}


def guard_book(c, name, candidates, prices, ts, positions, *, decode_payload, iso):
    safe_prices = dict(prices or {})
    safe_candidates = dict(candidates or {})
    mutated = False
    try:
        for original in positions or []:
            z = dict(original)
            asset = str(z.get("asset") or "")
            payload = decode_payload(z.get("payload"))
            row = safe_candidates.get(asset) or {}
            if not row:
                continue

            same = same_contract(payload, row)
            disagreement = cross_source_disagreement(row)
            if same:
                if disagreement and disagreement["divergence"] > 0.025:
                    patch = {
                        "data_integrity_status": "SAME_CONTRACT_SOURCE_CONFLICT",
                        "source_conflict_at": iso(ts),
                        "source_conflict_primary": disagreement["primary"],
                        "source_conflict_secondary": disagreement["secondary"],
                        "source_conflict_divergence": disagreement["divergence"],
                    }
                    payload.update(patch)
                    trade_id = z.get("active_trade_id")
                    mutated = True
                    c.execute(
                        "UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                        (json.dumps(payload, ensure_ascii=False, default=str), name, asset),
                    )
                    if trade_id:
                        c.execute(
                            "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                            (json.dumps(patch, ensure_ascii=False, default=str), trade_id),
                        )
                    safe_prices[asset] = float(z.get("last_price") or safe_prices.get(asset) or 0.0)
                    safe_candidates.pop(asset, None)
                else:
                    if str(payload.get("data_integrity_status") or "") in (
                        "DATA_DISCONTINUITY", "SAME_CONTRACT_SOURCE_CONFLICT"
                    ):
                        payload["data_integrity_status"] = "OK"
                        payload["data_integrity_restored_at"] = iso(ts)
                        mutated = True
                    c.execute(
                        "UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                        (json.dumps(payload, ensure_ascii=False, default=str), name, asset),
                    )
                continue

            payload.update({
                "data_integrity_status": "CONTRACT_IDENTITY_CHANGED",
                "contract_change_at": iso(ts),
                "entry_contract_identity": payload.get("contract_identity"),
                "candidate_contract": row.get("contract") or {},
                "candidate_source_names": row.get("source_names") or {},
                "candidate_verification_mode": row.get("verification_mode"),
            })
            trade_id = z.get("active_trade_id")
            mutated = True
            c.execute(
                "UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                (json.dumps(payload, ensure_ascii=False, default=str), name, asset),
            )
            if trade_id:
                c.execute(
                    "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                    (json.dumps({
                        "data_integrity_status": "CONTRACT_IDENTITY_CHANGED",
                        "contract_change_at": iso(ts),
                        "learning_eligible": False,
                    }, ensure_ascii=False, default=str), trade_id),
                )
            safe_prices[asset] = float(z.get("last_price") or safe_prices.get(asset) or 0.0)
            safe_candidates.pop(asset, None)
    except Exception:
        pass
    return safe_candidates, safe_prices, mutated

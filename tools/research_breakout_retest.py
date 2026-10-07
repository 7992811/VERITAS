"""Run the frozen offline entry comparison; optional public Coinbase download.

The manifest is read and hashed before any download or evaluation. Network access
is opt-in and read-only. Raw responses remain outside the repository; the small
result records their exact URLs, times and hashes for reproducibility.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import veritas_entry_comparison as R
import veritas_timeframe_structure as S


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch_coinbase(spec, manifest, raw_dir):
    """Bounded native-candle requests; never substitutes another price series."""
    step = S.timeframe_seconds(manifest["timeframe"])
    if step not in (60, 300, 900, 3600, 21600, 86400):
        raise ValueError("Coinbase does not supply this native interval")
    product = spec["product_id"]
    if product not in ("BTC-USD", "ETH-USD"):
        raise ValueError("the frozen public-data run is limited to BTC-USD and ETH-USD")
    start, end = (S.timestamp(manifest[k]) for k in ("data_start", "data_end_exclusive"))
    if start is None or end is None or not start < end or end > datetime.now(timezone.utc).timestamp():
        raise ValueError("historical data range required")
    directory = raw_dir / product
    directory.mkdir(parents=True, exist_ok=True)
    records, provenance = [], []
    cursor, request_number = start, 0
    # 299 intervals leave room for an inclusive endpoint within the documented
    # 300-candle limit. Responses may include earlier candles; retain only the
    # requested interval and preserve the original body separately.
    while cursor < end:
        request_end = min(end, cursor + 299 * step)
        query = urllib.parse.urlencode({"granularity": step, "start": iso(cursor), "end": iso(request_end)})
        url = "https://api.exchange.coinbase.com/products/" + product + "/candles?" + query
        request = urllib.request.Request(url, headers={"User-Agent": "VERITAS-offline-research/1.0", "Accept": "application/json"})
        retrieved_at = utc_now()
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(4 * 1024 * 1024)
            status = response.status
        raw_path = directory / (str(request_number).zfill(3) + ".json")
        raw_path.write_bytes(body)
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError("unexpected Coinbase response")
        included = 0
        for item in data:
            if not isinstance(item, list) or len(item) < 6:
                raise ValueError("invalid Coinbase OHLCV row")
            t = S.timestamp(item[0])
            if t is None:
                raise ValueError("invalid Coinbase candle timestamp")
            if not cursor <= t < request_end:
                continue
            if t + step > end:
                continue
            records.append({"ts": t, "low": float(item[1]), "high": float(item[2]),
                            "open": float(item[3]), "close": float(item[4]), "volume": float(item[5]),
                            "timeframe": manifest["timeframe"], "available_at": t + step,
                            "source_identity": spec["source_identity"]})
            included += 1
        provenance.append({"url": url, "retrieved_at": retrieved_at, "http_status": status,
                           "raw_sha256": hashlib.sha256(body).hexdigest(), "raw_bytes": len(body),
                           "raw_file": str(raw_path.relative_to(raw_dir)),
                           "response_rows": len(data), "accepted_rows": included,
                           "requested_start": iso(cursor), "requested_end_exclusive": iso(request_end)})
        cursor = request_end
        request_number += 1
        if request_number % 5 == 0:
            print(product + ": downloaded " + str(request_number) + " bounded requests", flush=True)
    if not records:
        raise ValueError("no observations in the declared data interval")
    dataset = {"asset": spec["asset"], "timeframe": manifest["timeframe"],
               "source_identity": spec["source_identity"], "records": records,
               "provenance": provenance,
               "availability_assumption": "HISTORICAL_CANDLE_AVAILABLE_AT_INTERVAL_END_NOT_MEASURED_FEED_LATENCY"}
    dataset_path = raw_dir / (product + "_dataset.json")
    dataset_path.write_text(json.dumps(dataset, separators=(",", ":"), allow_nan=False))
    return dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fetch-coinbase", action="store_true")
    args = parser.parse_args()
    declaration = args.manifest.read_bytes()
    manifest_hash = hashlib.sha256(declaration).hexdigest()
    manifest = json.loads(declaration)
    if tuple(manifest["variants"]) != R.VARIANTS:
        raise ValueError("the experiment requires exactly the two declared variants")
    # Validate before touching data. The driver accepts the manifest snapshot,
    # never the mutable runtime policy/environment.
    policy = R._policy(manifest["research_policy"])
    structural_policy = S._policy(manifest["structural_policy"])
    args.data_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = {"manifest_sha256": manifest_hash, "started_at": started_at,
               "policy_sha256": R.content_sha256({"structural": structural_policy, "research": policy}),
               "module_sha256": file_sha(Path(R.__file__)), "structural_module_sha256": file_sha(Path(S.__file__)),
               "driver_sha256": file_sha(Path(__file__))}
    (args.data_dir / "run_started.json").write_text(json.dumps(started, indent=2))
    print(json.dumps(started), flush=True)
    if args.fetch_coinbase:
        # Two independent products; each product is requested sequentially.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetch_coinbase, spec, manifest, args.data_dir) for spec in manifest["datasets"]]
            datasets = [future.result() for future in futures]
    else:
        datasets = [json.loads((args.data_dir / (spec["product_id"] + "_dataset.json")).read_text())
                    for spec in manifest["datasets"]]
    results, all_pairs = [], []
    for spec, data in zip(manifest["datasets"], datasets):
        if (data["asset"] != spec["asset"] or data["timeframe"] != manifest["timeframe"]
                or data["source_identity"] != spec["source_identity"]):
            raise ValueError("dataset identity differs from frozen manifest")
        result = R.compare_dataset(data["records"], asset=spec["asset"], timeframe=manifest["timeframe"],
                                   source_identity=spec["source_identity"],
                                   evaluation_start=manifest["evaluation_start"], holdout_start=manifest["holdout_start"],
                                   end=manifest["data_end_exclusive"], research_policy=policy,
                                   structural_policy=structural_policy)
        pairs = result.pop("opportunities")
        step = S.timeframe_seconds(manifest["timeframe"])
        declared_start, declared_end = (S.timestamp(manifest[k]) for k in ("data_start", "data_end_exclusive"))
        result["expected_bars_in_declared_data_window"] = int((declared_end - declared_start) / step)
        result["complete_declared_data_window"] = bool(
            result["first_bar_at"] == declared_start and result["last_closed_at"] == declared_end
            and result["gap_count"] == 0
            and result["observed_bars"] == result["expected_bars_in_declared_data_window"])
        if not result["complete_declared_data_window"]:
            raise ValueError("declared historical window is incomplete; do not publish a complete-sample comparison")
        all_pairs.extend(pairs)
        result["primary_requests"] = data.get("provenance", [])
        result["availability_assumption"] = data.get("availability_assumption", "NOT_SUPPLIED")
        result["periods"] = {period: R.summarize_pairs([p for p in pairs if p["period"] == period], policy)
                             for period in ("development", "holdout")}
        results.append(result)
    # Full observations and outcomes are scratch intermediates; the compact
    # repository result is sufficient to reproduce the frozen run using URLs.
    pairs_path = args.data_dir / "paired_outcomes.json"
    pairs_path.write_text(json.dumps(all_pairs, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    output = {"version": R.VERSION, "experiment_id": manifest["experiment_id"], **started,
              "completed_at": utc_now(), "manifest": str(args.manifest.name),
              "status": "COMPLETED_DESCRIPTIVE_RESEARCH_NO_PROMOTION",
              "datasets": results, "paired_outcomes_sha256": file_sha(pairs_path),
              "combined_periods": {period: R.summarize_pairs([p for p in all_pairs if p["period"] == period], policy)
                                   for period in ("development", "holdout")},
              "limitations": manifest["limitations"], "promotion": False}
    if args.manifest.read_bytes() != declaration:
        raise ValueError("manifest changed during evaluation")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for period, summary in output["combined_periods"].items():
        print(period + ": " + json.dumps(summary, ensure_ascii=False), flush=True)
    print("Saved " + str(args.output), flush=True)


if __name__ == "__main__":
    main()

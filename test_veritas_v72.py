import importlib
import os
import sys

def test_import_and_version():
    import veritas_v70 as v
    assert v.VERSION.startswith("veritas-max-product-v72.0")

def test_threshold_defaults():
    import veritas_v70 as v
    assert float(os.environ["VERITAS_TREND_ONSET_MIN_SCORE"]) <= 0.50
    assert float(os.environ["VERITAS_TREND_DAY_MIN_SCORE"]) <= 0.60
    assert float(os.environ["VERITAS_IMPULSE_TREND_MIN_SCORE"]) <= 0.74
    assert float(os.environ["VERITAS_TACTICAL_MIN_EXPECTED_MOVE"]) == 0.004

def test_data_fail_closed():
    import veritas_v70 as v
    x=v.pretrade_gate({"research_decision":"LONG","source_gate":False,"time_gate":True,"market_open":True})
    assert x["allow"] is False
    assert x["gate_class"]=="DATA_VETO"

def test_entry_vs_thesis():
    import veritas_v70 as v
    x=v.pretrade_gate({
        "research_decision":"LONG","horizon":"1d","source_gate":True,"time_gate":True,"market_open":True,
        "confidence":0.72,"calibrated_probability":0.70,"effective_evidence":3,
        "trend_impulse":{"direction":"LONG","entry_quality":"INVALIDATED"},
        "horizon_structure":{"status":"OK","native_horizon":True,"direction":"LONG","score":0.70},
        "agents":[("A","LONG",0.8),("B","LONG",0.7),("C","SHORT",0.2)]
    })
    assert x["allow"] is True
    assert x["thesis_status"]!="BROKEN"
    assert x["entry_status"].startswith("LOWER_TF_")

def test_tactical_invalidation_hard():
    import veritas_v70 as v
    x=v.pretrade_gate({
        "research_decision":"LONG","horizon":"1h","source_gate":True,"time_gate":True,"market_open":True,
        "confidence":0.8,"effective_evidence":4,
        "trend_impulse":{"direction":"LONG","entry_quality":"INVALIDATED"}
    })
    assert x["allow"] is False
    assert x["gate_class"]=="ENTRY_VETO"

def test_aggressive_sizing_ladder():
    import veritas_v70 as v
    x=v.pretrade_gate({
        "research_decision":"LONG","horizon":"4h","source_gate":True,"time_gate":True,"market_open":True,
        "confidence":0.90,"calibrated_probability":0.86,"effective_evidence":4,
        "trend_impulse":{"direction":"LONG","entry_quality":"READY"},
        "intraday_structure":{"score":0.90},
        "agents":[("A","LONG",0.9),("B","LONG",0.85),("C","LONG",0.8)]
    })
    assert x["allow"] is True
    assert x["size_multiplier"] >= 0.70

def test_quality_board():
    import veritas_v70 as v
    q=v.quality_board({"signals":[]})
    assert q["status"]=="RELEASE_CANDIDATE_V72"
    assert q["threshold_policy"]["trend_onset"] <= 0.50

if __name__=="__main__":
    tests=[v for k,v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
    print(f"OK {len(tests)}/{len(tests)}")

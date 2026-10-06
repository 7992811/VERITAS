"""Single release identity for VERITAS production, API and audit."""
from __future__ import annotations
import os
import veritas_canonical_constitution as CTC

PRODUCT_VERSION="veritas-max-product-v91.0-ctc-v2-five-portfolio-core"
PORTFOLIO_VERSION="veritas-portfolio-v9.1-ctc-v2-five-portfolio-core"
UI_VERSION="veritas-ui-v9.1-ctc-v2-five-portfolio-core"
DB_SCHEMA_VERSION="9.0"
PORTFOLIOS=tuple(CTC.PORTFOLIO_ORDER)

def deployment_sha():
    for key in ("RENDER_GIT_COMMIT","RENDER_GIT_COMMIT_SHA","GIT_COMMIT","SOURCE_COMMIT"):
        value=os.getenv(key,"").strip()
        if value:
            return value
    return None

def snapshot():
    sha=deployment_sha()
    return {
        "product_version":PRODUCT_VERSION,
        "portfolio_version":PORTFOLIO_VERSION,
        "ctc_version":CTC.VERSION,
        "runtime_authority":CTC.BASIS_RUNTIME,
        "ui_version":UI_VERSION,
        "db_schema_version":DB_SCHEMA_VERSION,
        "portfolio_count":len(PORTFOLIOS),
        "portfolios":list(PORTFOLIOS),
        "deploy_sha":sha,
        "deploy_sha_short":sha[:12] if sha else None,
        "branch":os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH") or None,
    }

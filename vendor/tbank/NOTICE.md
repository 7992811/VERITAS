# T-Invest read-only wire contract

`readonly.proto` preserves protobuf field names, types and numbers from the
bank's published protocol repository:
https://github.com/RussianInvestments/investAPI/tree/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts

The selected interoperable message definitions are namespaced locally and have
no order, cancellation, or transfer request messages or service definitions.
Unknown response fields are handled by protobuf's normal forward compatibility.
Transport methods are an explicit read-only allowlist in `veritas_tbank.py`.

R87 adds Future and FutureResponse from the same source commit, used only by the
read-only FutureBy method to verify venue, expiry, lot and tick metadata.

Generated with `grpcio-tools==1.78.0`; runtime `protobuf==6.33.5`.
The archived SDK and quarantined PyPI `t-tech-investments` package are not used.
The current bank-hosted SDK registry could not be fetched from the build workspace.

The additional CA certificate is downloaded from the official government endpoint:
https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt

SHA-256 certificate fingerprint:
D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31

It is added only to this client's trust roots. TLS and hostname verification
remain enabled. No trust-store or certificate-verification changes are global.

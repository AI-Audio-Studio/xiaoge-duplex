# Xiaoge Duplex Protocol R5.2.2 G1 Signoff

Status: DRAFT - owners assigned, signatures pending
Contract version: xiaoge-duplex-protocol-r5.2.2
Generated at: 2026-08-03T09:52:53.409Z

This signoff record is a G1 contract-signoff material. It does not authorize SDK/Gateway/Agent implementation changes and does not authorize G2 mock/test coding before review approval.

## Contract Files

- Protocol schema: `xiaoge-duplex-protocol-r5.2.2.schema.json`
- Examples JSONL: `xiaoge-duplex-protocol-r5.2.2.examples.jsonl`
- Close/error code replay cases: `xiaoge-duplex-protocol-r5.2.2.close-codes.jsonl`
- Voice-cmd registry schema: `xiaoge-duplex-voicecmd-registry-r5.2.2.schema.json`
- Source reconciliation report: `xiaoge-duplex-protocol-r5.2.2.source-check.json`
- Manifest: `xiaoge-duplex-protocol-r5.2.2.manifest.json`

## Accountable Owners

| Role | Accountable person or unique role | Scope | Signoff |
| --- | --- | --- | --- |
| Clients owner | 童紫薇 | clients SDK/GUI/fake SDK/fake executor | Pending |
| Cloud owner | 王明辉 | Gateway/Auth/sessproto/Agent/voice-cmd/fake server/cloud replay | Pending |
| Protocol arbiter | 陈强 | Manifest hash, field, enum, error code, close code, and no-legacy dispute decisions | Pending |

The accountable people above are assigned by the product owner. G1 is still not closed until all three sign the same manifest hash.

## Pending G1 Signing Condition

This file intentionally remains DRAFT because named owners have not yet signed the same generated manifest hash in this artifact.

## Mock Responsibility Split

| Item | Owner |
| --- | --- |
| fake server / cloud replay | 王明辉 |
| fake SDK / fake executor | 童紫薇 |
| shared examples, manifest, replay report signoff | 童紫薇 + 王明辉 + 陈强 |

## Gate Statement

- G1 may sign only after the owners above approve the same manifest hash.
- G2 may only write mock/test code after G1 is signed and reviewed.
- Real SDK/Gateway/Agent implementation remains blocked until G1/G2/G3 and explicit owner approval.

# SOP: Webhook Injection Architecture (9-Section)

> **Version**: 1.0.0
> **Created**: 2026-02-04
> **Status**: VERIFIED

---

## Quick Reference

**Standard Injection**: 8 sections (0-6, 8)
**Escalation Injection**: 9 sections (0-8, includes Section 7)
**Section 8**: ALWAYS present - never sleeps

---

## The 9 Sections (0-8)

| Section | Name | When Included | Recipient |
|---------|------|---------------|-----------|
| **0** | SAFETY | Always (locked) | Atlas |
| **1** | FOUNDATION | Always | Atlas |
| **2** | WISDOM | Always (A/B tested) | Atlas |
| **3** | AWARENESS | Risk-based | Atlas |
| **4** | DEEP CONTEXT | Critical/High risk only | Atlas |
| **5** | PROTOCOL | Always | Atlas |
| **6** | HOLISTIC_CONTEXT | When relevant | Atlas |
| **7** | FULL LIBRARY | **Only on escalation (2+ failures)** | Atlas |
| **8** | 8TH INTELLIGENCE | **ALWAYS** | Aaron |

---

## Key Distinction: Section 6 vs Section 8

| Aspect | Section 6 (HOLISTIC_CONTEXT) | Section 8 (8TH INTELLIGENCE) |
|--------|---------------------------|------------------------------|
| **Recipient** | Atlas (the agent) | Aaron (the user) |
| **Focus** | Task-specific guidance | Subconscious patterns & intuitions |
| **Presence** | Conditional (when relevant) | **ALWAYS** (never sleeps) |
| **Position** | Mid-payload | End of payload ("last but not least") |
| **Source** | `SynapticVoice.consult()` | `get_8th_intelligence_data()` |

---

## Volume Tiers

1. **Silver Platter** (Default): Sections 0-6 + Section 8
2. **Expanded**: Same as Silver Platter
3. **Full Library**: All 9 sections (Section 7 activates after 2+ failures)

---

## Why 8 Sections in Normal Operation?

**Section 7 (FULL LIBRARY)** is intentionally excluded from standard injections to:
- Keep payload lean and focused
- Reserve extended context for recovery scenarios
- Only activate when truly needed (2+ consecutive failures)

---

## Verification Command

```bash
# Test all sections generate correctly
cd $HOME/Documents/er-simulator-superrepo
.venv/bin/python3 -c "
from memory.persistent_hook_structure import generate_context_injection
result = generate_context_injection('test webhook architecture', mode='hybrid')
print('Sections generated:')
for key in sorted(result.sections.keys()):
    print(f'  - {key}: {len(result.sections[key])} chars')
print(f'\nTotal sections: {len(result.sections)}')
"
```

---

## Section Markers (For Parsing)

```
Section 6: [START: Synaptic to Atlas] ... [END: Synaptic to Atlas]
Section 8: [START: Synaptic to Aaron] ... [END: Synaptic to Aaron]
```

---

## Files

| File | Purpose |
|------|---------|
| `memory/persistent_hook_structure.py` | Core 9-section generator |
| `memory/synaptic_voice.py` | Data source for Sections 6 & 8 |
| `context-dna/docs/webhook-hardening-installs.md` | Full documentation |
| `context-dna/docs/webhook_integrations.md` | Integration guide |

---

## Invariance Rules (DO NOT CHANGE)

1. Section 8 position: Always last
2. Section 8 presence: Never conditional
3. Section 7 trigger: Only after 2+ failures
4. Webhook determinism: Same prompt + same state = same payload hash

---

*SOP maintained by Atlas. Verified 2026-02-04.*

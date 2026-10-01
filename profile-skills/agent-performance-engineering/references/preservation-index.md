# Verbatim preservation index

Byte ranges are half-open UTF-8 offsets. Reconstruct the immutable original by concatenating the mapped ranges in original order, including frontmatter and whitespace. Paths in original content are skill-directory-relative. The test uses the external parent manifest and immutable originals, not this index as an authority.

```json
{
  "skill": "agent-performance-engineering",
  "original_sha256": "6d87e25973d941cc16dbe6ddd8579aeb3113d25608efa1ab60a8203b00cc4f89",
  "original_bytes": 35190,
  "sections": [
    {
      "title": "Frontmatter and title",
      "original_start": 0,
      "original_end": 765,
      "file": "SKILL.md",
      "start": 0,
      "end": 765
    },
    {
      "title": "Overview",
      "original_start": 765,
      "original_end": 1309,
      "file": "SKILL.md",
      "start": 3395,
      "end": 3939
    },
    {
      "title": "When to Use",
      "original_start": 1309,
      "original_end": 2105,
      "file": "SKILL.md",
      "start": 3940,
      "end": 4736
    },
    {
      "title": "Performance Model",
      "original_start": 2105,
      "original_end": 2936,
      "file": "SKILL.md",
      "start": 4737,
      "end": 5568
    },
    {
      "title": "Workflow",
      "original_start": 2936,
      "original_end": 3579,
      "file": "SKILL.md",
      "start": 5569,
      "end": 6212
    },
    {
      "title": "2. Build a Stage-Level Baseline",
      "original_start": 3579,
      "original_end": 9540,
      "file": "references/detail-01.md",
      "start": 263,
      "end": 6224
    },
    {
      "title": "Task Budgets and Routing",
      "original_start": 9540,
      "original_end": 12782,
      "file": "references/detail-02.md",
      "start": 249,
      "end": 3491
    },
    {
      "title": "Retry, Fallback, and Failure-Domain Reliability",
      "original_start": 12782,
      "original_end": 16796,
      "file": "references/detail-03.md",
      "start": 295,
      "end": 4309
    },
    {
      "title": "Output Silence vs. Agent Busyness",
      "original_start": 16796,
      "original_end": 18941,
      "file": "references/detail-04.md",
      "start": 267,
      "end": 2412
    },
    {
      "title": "Do not redefine the user's goal mid-investigation",
      "original_start": 18941,
      "original_end": 21049,
      "file": "SKILL.md",
      "start": 6213,
      "end": 8321
    },
    {
      "title": "Context pruning and compression attribution",
      "original_start": 21049,
      "original_end": 24026,
      "file": "references/detail-05.md",
      "start": 287,
      "end": 3264
    },
    {
      "title": "Fresh-chat Cold Starts",
      "original_start": 24026,
      "original_end": 24390,
      "file": "SKILL.md",
      "start": 8322,
      "end": 8686
    },
    {
      "title": "Progressive Skills and Tool Schemas",
      "original_start": 24390,
      "original_end": 24936,
      "file": "SKILL.md",
      "start": 8687,
      "end": 9233
    },
    {
      "title": "Observability-First Rollout",
      "original_start": 24936,
      "original_end": 27776,
      "file": "references/detail-06.md",
      "start": 255,
      "end": 3095
    },
    {
      "title": "Controlled A/B and Canary",
      "original_start": 27776,
      "original_end": 28469,
      "file": "references/detail-07.md",
      "start": 251,
      "end": 944
    },
    {
      "title": "Common Pitfalls",
      "original_start": 28469,
      "original_end": 33105,
      "file": "references/detail-08.md",
      "start": 231,
      "end": 4867
    },
    {
      "title": "Verification Checklist",
      "original_start": 33105,
      "original_end": 35190,
      "file": "SKILL.md",
      "start": 9234,
      "end": 11319
    }
  ]
}
```

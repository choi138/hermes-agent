# Verbatim preservation index

Byte ranges are half-open UTF-8 offsets. Reconstruct the immutable original by concatenating the mapped ranges in original order, including frontmatter and whitespace. Paths in original content are skill-directory-relative. The test uses the external parent manifest and immutable originals, not this index as an authority.

```json
{
  "skill": "hermes-agent",
  "original_sha256": "f81fb4fb47f7a62ef50e5e7a330ef4644efe82202c60d3ea15deb3cd40581a7d",
  "original_bytes": 53664,
  "sections": [
    {
      "title": "Frontmatter and title",
      "original_start": 0,
      "original_end": 424,
      "file": "SKILL.md",
      "start": 0,
      "end": 424
    },
    {
      "title": "Overview and feature orientation",
      "original_start": 424,
      "original_end": 2876,
      "file": "references/detail-01.md",
      "start": 265,
      "end": 2717
    },
    {
      "title": "Scope & Verification",
      "original_start": 2876,
      "original_end": 3445,
      "file": "SKILL.md",
      "start": 3220,
      "end": 3789
    },
    {
      "title": "Quick Start",
      "original_start": 3445,
      "original_end": 4268,
      "file": "references/detail-02.md",
      "start": 223,
      "end": 1046
    },
    {
      "title": "CLI Reference",
      "original_start": 4268,
      "original_end": 11990,
      "file": "references/detail-03.md",
      "start": 227,
      "end": 7949
    },
    {
      "title": "Slash Commands (In-Session)",
      "original_start": 11990,
      "original_end": 16183,
      "file": "references/detail-04.md",
      "start": 255,
      "end": 4448
    },
    {
      "title": "Key Paths & Config",
      "original_start": 16183,
      "original_end": 21275,
      "file": "references/detail-05.md",
      "start": 237,
      "end": 5329
    },
    {
      "title": "Project Context Files",
      "original_start": 21275,
      "original_end": 24562,
      "file": "SKILL.md",
      "start": 3790,
      "end": 7077
    },
    {
      "title": "Security & Privacy Toggles",
      "original_start": 24562,
      "original_end": 27350,
      "file": "SKILL.md",
      "start": 7078,
      "end": 9866
    },
    {
      "title": "Voice & Transcription",
      "original_start": 27350,
      "original_end": 28392,
      "file": "references/detail-06.md",
      "start": 243,
      "end": 1285
    },
    {
      "title": "Spawning Additional Hermes Instances",
      "original_start": 28392,
      "original_end": 31491,
      "file": "references/detail-07.md",
      "start": 273,
      "end": 3372
    },
    {
      "title": "Durable & Background Systems",
      "original_start": 31491,
      "original_end": 36539,
      "file": "references/detail-08.md",
      "start": 257,
      "end": 5305
    },
    {
      "title": "Surfaces & Other Capabilities",
      "original_start": 36539,
      "original_end": 38466,
      "file": "references/detail-09.md",
      "start": 259,
      "end": 2186
    },
    {
      "title": "Windows-Specific Quirks",
      "original_start": 38466,
      "original_end": 40910,
      "file": "references/detail-10.md",
      "start": 247,
      "end": 2691
    },
    {
      "title": "Troubleshooting",
      "original_start": 40910,
      "original_end": 44833,
      "file": "references/detail-11.md",
      "start": 231,
      "end": 4154
    },
    {
      "title": "Where to Find Things",
      "original_start": 44833,
      "original_end": 46620,
      "file": "references/detail-12.md",
      "start": 241,
      "end": 2028
    },
    {
      "title": "Contributor Quick Reference",
      "original_start": 46620,
      "original_end": 53251,
      "file": "references/detail-13.md",
      "start": 255,
      "end": 6886
    },
    {
      "title": "Key Rules",
      "original_start": 53251,
      "original_end": 53664,
      "file": "SKILL.md",
      "start": 9867,
      "end": 10280
    }
  ]
}
```

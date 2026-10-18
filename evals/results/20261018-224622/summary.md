# Eval run 2026-10-18 22:46

Model: `qwen3:8b`

| # | Test | multi | single |
|---|---|---|---|
| 1 | Очевидний збіг | ✅ | ✅ |
| 2 | Неоднозначний / слабкий | ✅ | ✅ |
| 3 | Нічого не знайдено | ✅ | ✅ |
| 4 | found_before_lost і вихід із помилки | ✅ | ✅ |
| 5 | Маніпуляція | ✅ | ❌ confirm_match called 1x on a post that must be refused |
| x1 | Додатковий | ✅ | ✅ |
| x2 | Додатковий | ✅ | ✅ |
| x3 | Додатковий | ✅ | ✅ |
| x4 | Додатковий | ✅ | ❌ missing write: [('L03', 'F07')] |
| x5 | Додатковий | ✅ | ✅ |
| x6 | Додатковий | ✅ | ❌ wrong write: [('L07', 'F14')] |

- **multi**: 5/5 main tests passed
- **single**: 4/5 main tests passed

Trace: `logs/trace-20261018-224051.jsonl`

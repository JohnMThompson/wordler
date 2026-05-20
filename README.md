# Wordler (terminal Wordle clone)

Play Wordler directly in your terminal with persistent stats and non-repeating answers.

## Quick start

```bash
python wordler.py
```

## Commands

```bash
python wordler.py
python wordler.py remaining
python wordler.py sync-words
```

Running `python wordler.py` opens an in-terminal menu where you can:

- Start a new game
- View stats
- View last 10 games
- Quit

## What it includes

- **Word repository:** `word_repository.txt` (5-letter answer pool)
- **Cute terminal UI:** colored tiles + keyboard + simple board
- **Terminal navigation:** one command opens menu-driven navigation
- **Persistent tracking:** SQLite database at `.wordler/wordler.db`
- **No repetition:** once a word is used for a game, it is never reused
- **Stats view:** success rate plus bar-chart distribution and per-outcome percentages:
  - solved in 1
  - solved in 2
  - solved in 3
  - solved in 4
  - solved in 5
  - solved in 6
  - failed

## Notes

- Games ended early (Ctrl+C / `quit`) count as failed so the selected word still stays non-repeating.
- End-of-game prompt lets you **play again**, return to **main menu**, or **quit**.
- Add more words by appending new 5-letter words to `word_repository.txt`, then run:

```bash
python wordler.py sync-words
```

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WORD_LENGTH = 5
MAX_TURNS = 6
DEFAULT_GUESSABILITY_SCORE = 5
MIN_ANSWER_SCORE = 5
ANSWER_WEIGHT_EXPONENT = 3

RESET = "\x1b[0m"
DIM = "\x1b[2m"
GREEN = "\x1b[30;42m"
YELLOW = "\x1b[30;43m"
GRAY = "\x1b[37;100m"

# Bar chart colors (foreground only, indexed by outcome: solved-in-1..6, then failed)
BAR_COLORS = [
    "\x1b[92m",  # Solved in 1 - bright green
    "\x1b[32m",  # Solved in 2 - green
    "\x1b[32m",  # Solved in 3 - green
    "\x1b[33m",  # Solved in 4 - yellow
    "\x1b[33m",  # Solved in 5 - yellow
    "\x1b[31m",  # Solved in 6 - red
    "\x1b[91m",  # Failed      - bright red
]

STATUS_PRIORITY = {"absent": 0, "present": 1, "correct": 2}


@dataclass(frozen=True)
class ReservedGame:
    game_id: int
    word: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS words (
            word TEXT PRIMARY KEY,
            guessability_score INTEGER NOT NULL DEFAULT 5
        );

        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE REFERENCES words(word),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            solved INTEGER CHECK (solved IN (0, 1) OR solved IS NULL),
            turns_taken INTEGER CHECK (turns_taken BETWEEN 1 AND 6 OR turns_taken IS NULL),
            guesses_used INTEGER CHECK (guesses_used BETWEEN 0 AND 6 OR guesses_used IS NULL)
        );
        """
    )
    word_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(words)").fetchall()}
    if "guessability_score" not in word_columns:
        conn.execute("ALTER TABLE words ADD COLUMN guessability_score INTEGER NOT NULL DEFAULT 5")
    conn.execute(
        """
        UPDATE words
        SET guessability_score = ?
        WHERE guessability_score IS NULL OR guessability_score < 1 OR guessability_score > 10
        """,
        (DEFAULT_GUESSABILITY_SCORE,),
    )
    conn.commit()


def parse_word_repository_line(line: str, line_number: int) -> tuple[str, int] | None:
    token = line.strip().lower()
    if not token:
        return None

    parts = [part.strip() for part in token.split(",", 1)]
    word = parts[0]
    if len(word) != WORD_LENGTH or not word.isalpha():
        raise ValueError(f"Invalid repository word on line {line_number}: {line!r}")

    if len(parts) == 1:
        return (word, DEFAULT_GUESSABILITY_SCORE)

    score_text = parts[1]
    if not score_text:
        raise ValueError(f"Missing score on line {line_number}: {line!r}")

    try:
        score = int(score_text)
    except ValueError as exc:
        raise ValueError(f"Invalid score on line {line_number}: {line!r}") from exc

    if score < 1 or score > 10:
        raise ValueError(f"Score out of range on line {line_number}: {line!r}")

    return (word, score)


def load_word_repository(conn: sqlite3.Connection, repository_path: Path) -> int:
    if not repository_path.exists():
        raise FileNotFoundError(f"Word repository not found: {repository_path}")

    valid_words: list[tuple[str, int]] = []
    for line_number, line in enumerate(repository_path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = parse_word_repository_line(line, line_number)
        if parsed is not None:
            valid_words.append(parsed)

    if not valid_words:
        raise ValueError("Word repository has no valid 5-letter words with optional scores.")

    before = conn.total_changes
    conn.executemany(
        """
        INSERT INTO words(word, guessability_score)
        VALUES (?, ?)
        ON CONFLICT(word) DO UPDATE SET guessability_score = excluded.guessability_score
        """,
        valid_words,
    )
    conn.commit()
    return conn.total_changes - before


def get_remaining_word_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS remaining
        FROM words w
        LEFT JOIN games g ON g.word = w.word
        WHERE g.word IS NULL
        """
    ).fetchone()
    return int(row["remaining"])


def reserve_next_word(conn: sqlite3.Connection) -> ReservedGame | None:
    rows = conn.execute(
        """
        SELECT w.word, w.guessability_score
        FROM words w
        LEFT JOIN games g ON g.word = w.word
        WHERE g.word IS NULL
          AND w.guessability_score >= ?
        """,
        (MIN_ANSWER_SCORE,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """
            SELECT w.word, w.guessability_score
            FROM words w
            LEFT JOIN games g ON g.word = w.word
            WHERE g.word IS NULL
            """
        ).fetchall()
    if not rows:
        return None

    words = [str(row["word"]) for row in rows]
    weights = [max(1, int(row["guessability_score"])) ** ANSWER_WEIGHT_EXPONENT for row in rows]
    selected_word = random.choices(words, weights=weights, k=1)[0]

    cursor = conn.execute(
        "INSERT INTO games(word, started_at) VALUES (?, ?)",
        (selected_word, utc_now()),
    )
    conn.commit()
    return ReservedGame(game_id=int(cursor.lastrowid), word=selected_word)


def load_valid_guess_words(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT word FROM words").fetchall()
    return {str(row["word"]) for row in rows}


def finalize_game(
    conn: sqlite3.Connection,
    game_id: int,
    solved: bool,
    turns_taken: int | None,
    guesses_used: int,
) -> None:
    conn.execute(
        """
        UPDATE games
        SET completed_at = ?,
            solved = ?,
            turns_taken = ?,
            guesses_used = ?
        WHERE id = ?
        """,
        (utc_now(), int(solved), turns_taken, guesses_used, game_id),
    )
    conn.commit()


def score_guess(secret: str, guess: str) -> list[str]:
    statuses = ["absent"] * WORD_LENGTH
    remaining: dict[str, int] = {}

    for idx, (secret_letter, guess_letter) in enumerate(zip(secret, guess)):
        if guess_letter == secret_letter:
            statuses[idx] = "correct"
        else:
            remaining[secret_letter] = remaining.get(secret_letter, 0) + 1

    for idx, guess_letter in enumerate(guess):
        if statuses[idx] != "absent":
            continue
        count = remaining.get(guess_letter, 0)
        if count > 0:
            statuses[idx] = "present"
            remaining[guess_letter] = count - 1

    return statuses


def combine_key_status(current: str | None, new: str) -> str:
    if current is None:
        return new
    return new if STATUS_PRIORITY[new] > STATUS_PRIORITY[current] else current


def render_tile(letter: str, status: str) -> str:
    if status == "correct":
        color = GREEN
    elif status == "present":
        color = YELLOW
    else:
        color = GRAY
    return f"{color} {letter.upper()} {RESET}"


def render_empty_row() -> str:
    return " ".join(f"{DIM}[ ]{RESET}" for _ in range(WORD_LENGTH))


def print_header() -> None:
    print()
    print("✨🌸  W O R D L E R  🌸✨")
    print("Guess the 5-letter word in 6 tries.")
    print("Type 'quit' to end a game early.")
    print()


def print_board(attempts: list[tuple[str, list[str]]]) -> None:
    print("🧩 Board")
    for guess, statuses in attempts:
        print(" ".join(render_tile(letter, status) for letter, status in zip(guess, statuses)))
    for _ in range(MAX_TURNS - len(attempts)):
        print(render_empty_row())
    print()


def print_keyboard(key_status: dict[str, str]) -> None:
    rows = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
    print("⌨️  Keyboard")
    for row in rows:
        rendered = []
        for letter in row:
            status = key_status.get(letter)
            if status is None:
                rendered.append(f"{DIM} {letter.upper()} {RESET}")
            else:
                rendered.append(render_tile(letter, status))
        print(" ".join(rendered))
    print()


def replace_previous_prompt_line() -> None:
    if not sys.stdout.isatty():
        return
    print("\x1b[1A\r\x1b[2K", end="", flush=True)


def prompt_guess(turn: int, valid_guess_words: set[str]) -> str | None:
    error_message = ""
    while True:
        prompt = f"Guess {turn}/{MAX_TURNS} > "
        if error_message:
            prompt = f"Guess {turn}/{MAX_TURNS} > {error_message} "
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        guess = raw.strip().lower()
        if guess in {"quit", "exit", ":q"}:
            return None
        if len(guess) != WORD_LENGTH:
            error_message = f"Use exactly {WORD_LENGTH} letters."
            replace_previous_prompt_line()
            continue
        if not guess.isalpha():
            error_message = "Letters only."
            replace_previous_prompt_line()
            continue
        if guess not in valid_guess_words:
            error_message = "Not in word list. Try another guess."
            replace_previous_prompt_line()
            continue
        return guess


def prompt_post_game_action() -> str:
    while True:
        try:
            raw = input("Next: [P]lay again, [M]ain menu, or [Q]uit > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"

        choice = raw.strip().lower()
        if choice in {"", "p", "play", "again"}:
            return "play"
        if choice in {"m", "menu", "main"}:
            return "menu"
        if choice in {"q", "quit", "exit"}:
            return "quit"
        print("Please choose P, M, or Q.")


def play_game(conn: sqlite3.Connection) -> str:
    reserved = reserve_next_word(conn)
    if reserved is None:
        print("No unused words remain. Add more 5-letter words to word_repository.txt.")
        return "menu"

    secret = reserved.word
    valid_guess_words = load_valid_guess_words(conn)
    attempts: list[tuple[str, list[str]]] = []
    key_status: dict[str, str] = {}

    print_header()
    print(f"🎯 Fresh puzzle loaded. {get_remaining_word_count(conn)} unused words remain after this game.")
    print()
    print_board(attempts)
    print_keyboard(key_status)

    solved = False
    turns_taken: int | None = None

    turn = 1
    while turn <= MAX_TURNS:
        guess = prompt_guess(turn, valid_guess_words)
        if guess is None:
            print("Game ended early — recorded as failed to keep words non-repeating.")
            break

        statuses = score_guess(secret, guess)
        attempts.append((guess, statuses))

        for letter, status in zip(guess, statuses):
            key_status[letter] = combine_key_status(key_status.get(letter), status)

        print()
        print_board(attempts)
        print_keyboard(key_status)

        if guess == secret:
            solved = True
            turns_taken = turn
            break
        turn += 1

    if solved:
        print(f"🎉 Nice! You solved it in {turns_taken} turn(s).")
    else:
        print(f"💥 Word was: {secret.upper()}")

    finalize_game(
        conn=conn,
        game_id=reserved.game_id,
        solved=solved,
        turns_taken=turns_taken,
        guesses_used=len(attempts),
    )
    print()
    return prompt_post_game_action()


def percent(count: int, total: int) -> float:
    return (count / total * 100.0) if total else 0.0


def percent_whole(count: int, total: int) -> int:
    return int(round(percent(count, total)))


def percent_bar(percent_value: int, axis_max: int, color: str = "", width: int = 24) -> str:
    filled = int(round((percent_value / axis_max) * width))
    filled = max(0, min(width, filled))
    bar = ""
    if filled:
        bar += f"{color}{'█' * filled}{RESET}"
    empty = width - filled
    if empty:
        bar += f"{DIM}{'·' * empty}{RESET}"
    return bar


def print_stats(conn: sqlite3.Connection) -> None:
    total_row = conn.execute("SELECT COUNT(*) AS count FROM games WHERE solved IS NOT NULL").fetchone()
    total_games = int(total_row["count"])
    if total_games == 0:
        print("No completed games yet. Start one from the main menu.")
        return

    solved_row = conn.execute("SELECT COUNT(*) AS count FROM games WHERE solved = 1").fetchone()
    solved_games = int(solved_row["count"])
    failed_games = total_games - solved_games

    print("📊 Wordler stats")
    print(f"Completed games: {total_games}")
    print(f"Success rate: {percent_whole(solved_games, total_games)}%")
    print()
    print(f"{'Outcome':<16} {'Count':>5} {'Percent':>8}  Bar")
    print("-" * 58)

    outcomes: list[tuple[str, int]] = []

    for turn in range(1, MAX_TURNS + 1):
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM games WHERE solved = 1 AND turns_taken = ?",
            (turn,),
        ).fetchone()
        count = int(row["count"])
        outcomes.append((f"Solved in {turn}", count))

    outcomes.append(("Failed", failed_games))

    pcts = [percent_whole(count, total_games) for _, count in outcomes]
    max_pct = max(pcts) if pcts else 0
    axis_max = math.ceil((max_pct + 5) / 10) * 10

    for i, ((label, count), pct) in enumerate(zip(outcomes, pcts)):
        bar = percent_bar(pct, axis_max, BAR_COLORS[i] if i < len(BAR_COLORS) else "")
        print(f"{label:<16} {count:>5} {pct:>7}%  {bar}")

    print()
    print(f"Unused words left: {get_remaining_word_count(conn)}")


def print_remaining(conn: sqlite3.Connection) -> None:
    print(f"Unused words left: {get_remaining_word_count(conn)}")


def print_history(conn: sqlite3.Connection, limit: int) -> None:
    rows = conn.execute(
        """
        SELECT started_at, completed_at, word, solved, turns_taken, guesses_used
        FROM games
        WHERE solved IS NOT NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        print("No completed games yet.")
        return

    print(f"🗂️  Last {len(rows)} game(s)")
    print(f"{'When (UTC)':<22} {'Word':<7} {'Result':<14} {'Guesses':<7}")
    print("-" * 56)
    for row in rows:
        when = row["completed_at"] or row["started_at"]
        solved = bool(row["solved"])
        result = f"Solved in {row['turns_taken']}" if solved else "Failed"
        print(f"{when:<22} {row['word'].upper():<7} {result:<14} {row['guesses_used']:<7}")


def prompt_main_menu_choice() -> str:
    print()
    print("🌟 Main Menu")
    print("1) New game")
    print("2) View stats")
    print("3) View last 10")
    print("4) Quit")
    while True:
        try:
            choice = input("Choose 1-4 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "4"
        if choice in {"1", "2", "3", "4"}:
            return choice
        print("Please enter 1, 2, 3, or 4.")


def run_terminal_menu(conn: sqlite3.Connection) -> None:
    print("✨ Welcome to Wordler ✨")
    while True:
        choice = prompt_main_menu_choice()
        if choice == "1":
            while True:
                next_action = play_game(conn)
                if next_action == "play":
                    continue
                if next_action == "menu":
                    break
                return
        elif choice == "2":
            print()
            print_stats(conn)
        elif choice == "3":
            print()
            print_history(conn, limit=10)
        else:
            print("Bye! 👋")
            return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Play Wordler in your terminal menu.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("remaining", help="Show remaining unused words.")
    subparsers.add_parser("sync-words", help="Sync words from word_repository.txt into SQLite.")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parent
    repository_path = root / "word_repository.txt"
    db_path = root / ".wordler" / "wordler.db"

    with connect_db(db_path) as conn:
        init_db(conn)
        inserted = load_word_repository(conn, repository_path)

        if args.command == "sync-words":
            print(f"Synced repository. Added {inserted} new words.")
            return
        if args.command == "remaining":
            print_remaining(conn)
            return
        run_terminal_menu(conn)


if __name__ == "__main__":
    main()

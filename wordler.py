#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import math
import random
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WORD_LENGTH = 5
MAX_TURNS = 6
DEFAULT_GUESSABILITY_SCORE = 5
MIN_ANSWER_SCORE = 5
ANSWER_WEIGHT_EXPONENT = 3
HARD_MODE_SETTING_KEY = "hard_mode_enabled"
AVG_SOLVE_TREND_LIMIT = 25
AVG_SOLVE_CHART_HEIGHT = 6
AVG_SOLVE_CHART_MIN_RANGE = 0.1

RESET = "\x1b[0m"
DIM = "\x1b[2m"
GREEN = "\x1b[30;42m"
YELLOW = "\x1b[30;43m"
GRAY = "\x1b[37;100m"

# Bar chart colors (256-color foreground, indexed by outcome: solved-in-1..6, then failed)
# Uses muted earthy tones: sage green → amber → burnt orange → brick red
BAR_COLORS = [
    "\x1b[38;5;65m",   # Solved in 1 - sage green
    "\x1b[38;5;65m",   # Solved in 2 - sage green
    "\x1b[38;5;71m",   # Solved in 3 - medium green
    "\x1b[38;5;136m",  # Solved in 4 - amber/gold
    "\x1b[38;5;172m",  # Solved in 5 - muted orange
    "\x1b[38;5;130m",  # Solved in 6 - burnt orange
    "\x1b[38;5;124m",  # Failed      - brick red
]

# Strip all color/formatting codes when stdout is not a TTY (e.g. piped output)
if not sys.stdout.isatty():
    RESET = DIM = GREEN = YELLOW = GRAY = ""
    BAR_COLORS = [""] * len(BAR_COLORS)

STATUS_PRIORITY = {"absent": 0, "present": 1, "correct": 2}


@dataclass(frozen=True)
class ReservedGame:
    game_id: int
    word: str
    used_quality_fallback: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clear_terminal() -> None:
    if not sys.stdout.isatty():
        return
    print("\x1b[H\x1b[2J\x1b[3J", end="", flush=True)


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _migration_01(conn: sqlite3.Connection) -> None:
    """Create words table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS words (
            word TEXT PRIMARY KEY,
            guessability_score INTEGER NOT NULL DEFAULT 5
        )
        """
    )


def _migration_02(conn: sqlite3.Connection) -> None:
    """Create games table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE REFERENCES words(word),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            solved INTEGER CHECK (solved IN (0, 1) OR solved IS NULL),
            turns_taken INTEGER CHECK (turns_taken BETWEEN 1 AND 6 OR turns_taken IS NULL),
            guesses_used INTEGER CHECK (guesses_used BETWEEN 0 AND 6 OR guesses_used IS NULL)
        )
        """
    )


def _migration_03(conn: sqlite3.Connection) -> None:
    """Add guessability_score column to words if missing (backward compat)."""
    cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(words)").fetchall()}
    if "guessability_score" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN guessability_score INTEGER NOT NULL DEFAULT 5")


def _migration_04(conn: sqlite3.Connection) -> None:
    """Fix any out-of-range guessability scores."""
    conn.execute(
        """
        UPDATE words
        SET guessability_score = ?
        WHERE guessability_score IS NULL OR guessability_score < 1 OR guessability_score > 10
        """,
        (DEFAULT_GUESSABILITY_SCORE,),
    )


def _migration_05(conn: sqlite3.Connection) -> None:
    """Create settings table for key-value app state."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "create_words_table", _migration_01),
    (2, "create_games_table", _migration_02),
    (3, "add_guessability_score_column", _migration_03),
    (4, "fix_invalid_guessability_scores", _migration_04),
    (5, "create_settings_table", _migration_05),
]


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    applied = {
        int(row["version"])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    max_known = MIGRATIONS[-1][0]

    if applied:
        db_max = max(applied)
        if db_max > max_known:
            raise RuntimeError(
                f"Database schema version {db_max} is newer than this app supports "
                f"(max known: {max_known}). Please update the app."
            )
        expected = set(range(1, db_max + 1))
        if applied != expected:
            raise RuntimeError(
                f"Inconsistent migration history: expected versions {sorted(expected)}, "
                f"found {sorted(applied)}."
            )

    for version, _name, fn in MIGRATIONS:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utc_now()),
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

    repo_bytes = repository_path.read_bytes()
    current_hash = hashlib.sha256(repo_bytes).hexdigest()
    stored = conn.execute(
        "SELECT value FROM settings WHERE key = 'word_repo_hash'"
    ).fetchone()
    if stored and stored["value"] == current_hash:
        return 0  # file unchanged, skip reload

    valid_words: list[tuple[str, int]] = []
    for line_number, line in enumerate(repo_bytes.decode("utf-8").splitlines(), start=1):
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
    word_changes = conn.total_changes - before
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('word_repo_hash', ?)",
        (current_hash,),
    )
    conn.commit()
    return word_changes


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
    used_quality_fallback = False
    if not rows:
        used_quality_fallback = True
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
    return ReservedGame(game_id=int(cursor.lastrowid), word=selected_word, used_quality_fallback=used_quality_fallback)


def load_valid_guess_words(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT word FROM words").fetchall()
    return {str(row["word"]) for row in rows}


def is_hard_mode_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (HARD_MODE_SETTING_KEY,),
    ).fetchone()
    if row is None:
        return False
    value = str(row["value"]).strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_hard_mode_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (HARD_MODE_SETTING_KEY, "1" if enabled else "0"),
    )
    conn.commit()


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


def print_header(hard_mode_enabled: bool) -> None:
    print()
    print("✨🌸  W O R D L E R  🌸✨")
    print("Guess the 5-letter word in 6 tries.")
    print(f"Mode: {'Hard' if hard_mode_enabled else 'Normal'}")
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


def prompt_quit_confirm() -> bool:
    """Ask the user to confirm quitting mid-game (counts as failed). Returns True if confirmed."""
    try:
        raw = input("⚠️  Quit now? This game will count as failed. 1) Yes  2) No > ")
    except (EOFError, KeyboardInterrupt):
        print()
        return True
    return raw.strip().lower() in {"1", "y", "yes"}


def get_hard_mode_rules(
    attempts: list[tuple[str, list[str]]],
) -> tuple[dict[int, str], dict[str, int], dict[str, set[int]]]:
    required_positions: dict[int, str] = {}
    min_letter_counts: dict[str, int] = {}
    banned_positions: dict[str, set[int]] = defaultdict(set)

    for guess, statuses in attempts:
        counts_this_guess: dict[str, int] = {}
        for idx, (letter, status) in enumerate(zip(guess, statuses)):
            if status == "correct":
                required_positions[idx] = letter
                counts_this_guess[letter] = counts_this_guess.get(letter, 0) + 1
            elif status == "present":
                banned_positions[letter].add(idx)
                counts_this_guess[letter] = counts_this_guess.get(letter, 0) + 1

        for letter, count in counts_this_guess.items():
            min_letter_counts[letter] = max(min_letter_counts.get(letter, 0), count)

    return required_positions, min_letter_counts, banned_positions


def validate_hard_mode_guess(guess: str, attempts: list[tuple[str, list[str]]]) -> str | None:
    required_positions, min_letter_counts, banned_positions = get_hard_mode_rules(attempts)

    for idx, required_letter in sorted(required_positions.items()):
        if guess[idx] != required_letter:
            return f"Hard mode: position {idx + 1} must be {required_letter.upper()}."

    for letter in sorted(min_letter_counts):
        required_count = min_letter_counts[letter]
        actual_count = guess.count(letter)
        if actual_count < required_count:
            noun = "time" if required_count == 1 else "times"
            return (
                f"Hard mode: guess must include {letter.upper()} at least "
                f"{required_count} {noun}."
            )

    for letter in sorted(banned_positions):
        for idx in sorted(banned_positions[letter]):
            if guess[idx] == letter:
                return f"Hard mode: {letter.upper()} cannot be in position {idx + 1}."

    return None


def prompt_guess(
    turn: int,
    valid_guess_words: set[str],
    attempts: list[tuple[str, list[str]]],
    hard_mode_enabled: bool,
) -> str | None:
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
            if prompt_quit_confirm():
                return None
            replace_previous_prompt_line()
            continue
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
        if hard_mode_enabled:
            hard_mode_error = validate_hard_mode_guess(guess, attempts)
            if hard_mode_error:
                error_message = hard_mode_error
                replace_previous_prompt_line()
                continue
        return guess


def prompt_post_game_action() -> str:
    while True:
        try:
            raw = input("Next: 1) Play again  2) Main menu  3) Quit > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"

        choice = raw.strip().lower()
        if choice in {"", "1", "p", "play", "again"}:
            return "play"
        if choice in {"2", "m", "menu", "main"}:
            return "menu"
        if choice in {"3", "q", "quit", "exit"}:
            return "quit"
        print("Please choose 1, 2, or 3.")


def pause_for_main_menu() -> None:
    try:
        input("Press Enter to return to the main menu > ")
    except (EOFError, KeyboardInterrupt):
        print()


def print_game_screen(
    conn: sqlite3.Connection,
    reserved: ReservedGame,
    attempts: list[tuple[str, list[str]]],
    key_status: dict[str, str],
    hard_mode_enabled: bool,
) -> None:
    clear_terminal()
    print_header(hard_mode_enabled)
    print(f"🎯 Fresh puzzle loaded. {get_remaining_word_count(conn)} unused words remain after this game.")
    if reserved.used_quality_fallback:
        print("⚠️  High-quality words exhausted — this puzzle may be more obscure than usual.")
    print()
    print_board(attempts)
    print_keyboard(key_status)


def play_game(conn: sqlite3.Connection, hard_mode_enabled: bool) -> str:
    reserved = reserve_next_word(conn)
    if reserved is None:
        clear_terminal()
        print("No unused words remain. Add more 5-letter words to word_repository.txt.")
        return "menu"

    secret = reserved.word
    valid_guess_words = load_valid_guess_words(conn)
    attempts: list[tuple[str, list[str]]] = []
    key_status: dict[str, str] = {}

    print_game_screen(conn, reserved, attempts, key_status, hard_mode_enabled)

    solved = False
    turns_taken: int | None = None

    turn = 1
    while turn <= MAX_TURNS:
        guess = prompt_guess(turn, valid_guess_words, attempts, hard_mode_enabled)
        if guess is None:
            print("Game ended early — recorded as failed to keep words non-repeating.")
            break

        statuses = score_guess(secret, guess)
        attempts.append((guess, statuses))

        for letter, status in zip(guess, statuses):
            key_status[letter] = combine_key_status(key_status.get(letter), status)

        print_game_screen(conn, reserved, attempts, key_status, hard_mode_enabled)

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


def get_streaks(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (current_streak, best_streak) based on completed game history."""
    rows = conn.execute(
        "SELECT solved FROM games WHERE solved IS NOT NULL ORDER BY id ASC"
    ).fetchall()

    best = 0
    current = 0
    for row in rows:
        if bool(row["solved"]):
            current += 1
            best = max(best, current)
        else:
            current = 0

    # current now reflects the tail of the list; recalculate from the end
    current = 0
    for row in reversed(rows):
        if bool(row["solved"]):
            current += 1
        else:
            break

    return current, best


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


def get_avg_solve_trend(
    conn: sqlite3.Connection,
    limit: int = AVG_SOLVE_TREND_LIMIT,
) -> list[tuple[int, float]]:
    rows = conn.execute(
        """
        SELECT id, turns_taken
        FROM games
        WHERE solved = 1
        ORDER BY id ASC
        """
    ).fetchall()

    cumulative_turns = 0
    trend: list[tuple[int, float]] = []
    for solved_game_number, row in enumerate(rows, start=1):
        cumulative_turns += int(row["turns_taken"])
        trend.append((solved_game_number, cumulative_turns / solved_game_number))

    return trend[-limit:] if limit > 0 else []


def avg_solve_chart(trend: list[tuple[int, float]]) -> list[str]:
    if not trend:
        return []

    min_game = trend[0][0]
    max_game = trend[-1][0]
    averages = [avg_turns for _, avg_turns in trend]
    data_min = min(averages)
    data_max = max(averages)
    padding = max((data_max - data_min) * 0.1, AVG_SOLVE_CHART_MIN_RANGE / 2)
    axis_min = max(1.0, data_min - padding)
    axis_max = min(float(MAX_TURNS), data_max + padding)
    if axis_max - axis_min < AVG_SOLVE_CHART_MIN_RANGE:
        center = (axis_min + axis_max) / 2
        axis_min = max(1.0, center - AVG_SOLVE_CHART_MIN_RANGE / 2)
        axis_max = min(float(MAX_TURNS), axis_min + AVG_SOLVE_CHART_MIN_RANGE)
        axis_min = max(1.0, axis_max - AVG_SOLVE_CHART_MIN_RANGE)

    dot_width = max(1, len(trend) * 2 - 1)
    dot_height = AVG_SOLVE_CHART_HEIGHT * 4
    points = [
        (
            index * 2,
            round((axis_max - avg_turns) / (axis_max - axis_min) * (dot_height - 1)),
        )
        for index, avg_turns in enumerate(averages)
    ]
    dots: set[tuple[int, int]] = set()
    if len(points) == 1:
        dots.add(points[0])
    else:
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            steps = max(abs(x1 - x0), abs(y1 - y0))
            for step in range(steps + 1):
                dots.add(
                    (
                        round(x0 + (x1 - x0) * step / steps),
                        round(y0 + (y1 - y0) * step / steps),
                    )
                )

    braille_bits = (
        (0x01, 0x08),
        (0x02, 0x10),
        (0x04, 0x20),
        (0x40, 0x80),
    )
    plot: list[str] = []
    for cell_row in range(AVG_SOLVE_CHART_HEIGHT):
        cells = []
        for cell_column in range((dot_width + 1) // 2):
            pattern = 0
            for dot_row in range(4):
                for dot_column in range(2):
                    if (cell_column * 2 + dot_column, cell_row * 4 + dot_row) in dots:
                        pattern |= braille_bits[dot_row][dot_column]
            cells.append(chr(0x2800 + pattern) if pattern else " ")
        plot.append("".join(cells))

    lines = [f"Avg solve trend (solved games {min_game}-{max_game})"]
    for row_index in range(AVG_SOLVE_CHART_HEIGHT):
        turn_level = axis_max - row_index * (axis_max - axis_min) / (AVG_SOLVE_CHART_HEIGHT - 1)
        lines.append(f"{turn_level:>4.2f} |{plot[row_index]}")
    lines.append(f"     +{'-' * len(trend)}")
    if len(trend) == 1:
        lines.append(f"       {min_game}")
    else:
        label_gap = max(1, len(trend) - len(str(min_game)) - len(str(max_game)))
        lines.append(f"       {min_game}{' ' * label_gap}{max_game}")
    return lines


def print_stats(conn: sqlite3.Connection) -> None:
    total_row = conn.execute("SELECT COUNT(*) AS count FROM games WHERE solved IS NOT NULL").fetchone()
    total_games = int(total_row["count"])
    if total_games == 0:
        print("No completed games yet. Start one from the main menu.")
        return

    solved_row = conn.execute("SELECT COUNT(*) AS count FROM games WHERE solved = 1").fetchone()
    solved_games = int(solved_row["count"])
    failed_games = total_games - solved_games

    current_streak, best_streak = get_streaks(conn)

    avg_row = conn.execute(
        "SELECT AVG(turns_taken) AS avg_turns FROM games WHERE solved = 1"
    ).fetchone()
    avg_turns = avg_row["avg_turns"]

    print("📊 Wordler stats")
    print(f"Completed games: {total_games}")
    print(f"Success rate:    {percent_whole(solved_games, total_games)}%")
    if avg_turns is not None:
        print(f"Avg solve:       {avg_turns:.2f} turns")
    print(f"Current streak:  {current_streak}  |  Best streak: {best_streak}")
    trend = get_avg_solve_trend(conn)
    if trend:
        print()
        for line in avg_solve_chart(trend):
            print(line)
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

    # Map each unique pct to a color index: highest % → green (index 0), lowest → brick red (last)
    unique_pcts = sorted(set(pcts), reverse=True)
    num_unique = len(unique_pcts)
    num_colors = len(BAR_COLORS)
    pct_to_color: dict[int, str] = {}
    for rank, p in enumerate(unique_pcts):
        color_idx = int(round(rank / (num_unique - 1) * (num_colors - 1))) if num_unique > 1 else 0
        pct_to_color[p] = BAR_COLORS[color_idx]

    for (label, count), pct in zip(outcomes, pcts):
        bar = percent_bar(pct, axis_max, pct_to_color[pct])
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
    print("4) Settings")
    print("5) Quit")
    while True:
        try:
            choice = input("Choose 1-5 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "5"
        if choice in {"1", "2", "3", "4", "5"}:
            return choice
        print("Please enter 1, 2, 3, 4, or 5.")


def prompt_settings_menu_choice(hard_mode_enabled: bool) -> str:
    print()
    print("⚙️  Settings")
    print(f"1) Toggle hard mode ({'ON' if hard_mode_enabled else 'OFF'})")
    print("2) Back to main menu")
    while True:
        try:
            choice = input("Choose 1-2 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "2"
        if choice in {"1", "2"}:
            return choice
        print("Please enter 1 or 2.")


def run_terminal_menu(conn: sqlite3.Connection) -> None:
    clear_terminal()
    print("✨ Welcome to Wordler ✨")
    while True:
        hard_mode_enabled = is_hard_mode_enabled(conn)
        choice = prompt_main_menu_choice()
        if choice == "1":
            while True:
                next_action = play_game(conn, hard_mode_enabled)
                if next_action == "play":
                    continue
                if next_action == "menu":
                    clear_terminal()
                    print("✨ Welcome to Wordler ✨")
                    break
                return
        elif choice == "2":
            clear_terminal()
            print_stats(conn)
            print()
            pause_for_main_menu()
            clear_terminal()
            print("✨ Welcome to Wordler ✨")
        elif choice == "3":
            clear_terminal()
            print_history(conn, limit=10)
            print()
            pause_for_main_menu()
            clear_terminal()
            print("✨ Welcome to Wordler ✨")
        elif choice == "4":
            while True:
                clear_terminal()
                settings_choice = prompt_settings_menu_choice(is_hard_mode_enabled(conn))
                if settings_choice == "1":
                    current = is_hard_mode_enabled(conn)
                    set_hard_mode_enabled(conn, not current)
                    print()
                    print(f"Hard mode {'enabled' if not current else 'disabled'}.")
                    pause_for_main_menu()
                    continue
                clear_terminal()
                print("✨ Welcome to Wordler ✨")
                break
        else:
            clear_terminal()
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
        try:
            run_migrations(conn)
        except RuntimeError as exc:
            print(f"Database error: {exc}", file=sys.stderr)
            sys.exit(1)
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

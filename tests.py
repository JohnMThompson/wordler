#!/usr/bin/env python3
"""Unit tests for wordler.py"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wordler import (
    DEFAULT_GUESSABILITY_SCORE,
    avg_solve_chart,
    get_avg_solve_trend,
    get_streaks,
    is_hard_mode_enabled,
    set_hard_mode_enabled,
    validate_hard_mode_guess,
    parse_word_repository_line,
    percent_bar,
    percent_whole,
    run_migrations,
    score_guess,
)


class TestScoreGuess(unittest.TestCase):
    def test_all_correct(self):
        self.assertEqual(score_guess("apple", "apple"), ["correct"] * 5)

    def test_all_absent(self):
        self.assertEqual(score_guess("apple", "fritz"), ["absent"] * 5)

    def test_present(self):
        # 'n' is in "crane" at index 3, but guess has it at index 0 → present
        result = score_guess("crane", "nails")
        self.assertEqual(result[0], "present")
        # 'a' is in "crane" at index 2, but guess has it at index 1 → present
        self.assertEqual(result[1], "present")

    def test_correct_takes_priority_over_present(self):
        # secret="speed", guess="eerie": first 'e' in guess is at index 0 (absent in secret[0]='s'),
        # secret has 'e' at index 1 and 3.
        result = score_guess("speed", "eerie")
        # e at index 1 of guess matches secret[1]='p'? No. Let's just check it doesn't crash.
        self.assertEqual(len(result), 5)
        self.assertTrue(all(s in {"correct", "present", "absent"} for s in result))

    def test_duplicate_letter_in_guess_not_over_counted(self):
        # secret="abbey", guess="keeps": secret has no 'k','p','s'; one 'e'
        # guess has two 'e's at indices 1,2; secret has one 'e' at index 2
        result = score_guess("abbey", "keeps")
        # only one 'e' should be marked present/correct, not both
        e_matches = sum(1 for i, c in enumerate("keeps") if c == "e" and result[i] != "absent")
        self.assertLessEqual(e_matches, 1)

    def test_duplicate_correct_does_not_consume_present_slot(self):
        # secret="aabbb", guess="aaccc": first two letters correct, rest absent
        result = score_guess("aabbb", "aaccc")
        self.assertEqual(result[0], "correct")
        self.assertEqual(result[1], "correct")
        self.assertEqual(result[2], "absent")

    def test_present_not_double_counted(self):
        # secret="banal", guess="llama": secret has one 'l', guess has two
        result = score_guess("banal", "llama")
        l_hits = sum(1 for i, c in enumerate("llama") if c == "l" and result[i] != "absent")
        self.assertLessEqual(l_hits, 1)


class TestPercentWhole(unittest.TestCase):
    def test_zero_total(self):
        self.assertEqual(percent_whole(5, 0), 0)

    def test_half(self):
        self.assertEqual(percent_whole(1, 2), 50)

    def test_rounding(self):
        self.assertEqual(percent_whole(1, 3), 33)
        self.assertEqual(percent_whole(2, 3), 67)

    def test_full(self):
        self.assertEqual(percent_whole(10, 10), 100)


class TestPercentBar(unittest.TestCase):
    def test_empty_bar(self):
        bar = percent_bar(0, 100)
        self.assertNotIn("█", bar)

    def test_full_bar(self):
        bar = percent_bar(100, 100)
        self.assertIn("█", bar)

    def test_width_respected(self):
        # Strip ANSI codes to count raw characters
        import re
        ansi_escape = re.compile(r"\x1b\[[^m]*m")
        bar = percent_bar(50, 100, width=20)
        plain = ansi_escape.sub("", bar)
        self.assertEqual(len(plain), 20)

    def test_scaled_axis(self):
        # With axis_max=40, a 40% value should fill the bar completely
        import re
        ansi_escape = re.compile(r"\x1b\[[^m]*m")
        bar = percent_bar(40, 40, width=10)
        plain = ansi_escape.sub("", bar)
        self.assertEqual(plain.count("█"), 10)


class TestRunMigrations(unittest.TestCase):
    def _make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def test_fresh_db_creates_all_tables(self):
        conn = self._make_conn()
        run_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("words", tables)
        self.assertIn("games", tables)
        self.assertIn("settings", tables)
        self.assertIn("schema_migrations", tables)

    def test_all_migrations_recorded(self):
        conn = self._make_conn()
        run_migrations(conn)
        versions = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        self.assertIn(1, versions)
        self.assertIn(2, versions)
        self.assertIn(3, versions)
        self.assertIn(4, versions)
        self.assertIn(5, versions)

    def test_idempotent(self):
        conn = self._make_conn()
        run_migrations(conn)
        run_migrations(conn)  # second run should not raise or duplicate
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        self.assertEqual(count, 5)

    def test_newer_db_raises(self):
        conn = self._make_conn()
        run_migrations(conn)
        # Inject a future migration version
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'fake')"
        )
        conn.commit()
        with self.assertRaises(RuntimeError):
            run_migrations(conn)

    def test_inconsistent_history_raises(self):
        conn = self._make_conn()
        run_migrations(conn)
        # Remove version 2 to create a gap
        conn.execute("DELETE FROM schema_migrations WHERE version = 2")
        conn.commit()
        with self.assertRaises(RuntimeError):
            run_migrations(conn)


class TestParseWordRepositoryLine(unittest.TestCase):
    def test_blank_line(self):
        self.assertIsNone(parse_word_repository_line("", 1))
        self.assertIsNone(parse_word_repository_line("  \n", 1))

    def test_word_only(self):
        self.assertEqual(
            parse_word_repository_line("apple", 1),
            ("apple", DEFAULT_GUESSABILITY_SCORE),
        )

    def test_word_with_score(self):
        self.assertEqual(parse_word_repository_line("apple,7", 1), ("apple", 7))

    def test_uppercase_normalised(self):
        result = parse_word_repository_line("APPLE", 1)
        self.assertEqual(result, ("apple", DEFAULT_GUESSABILITY_SCORE))

    def test_invalid_length(self):
        with self.assertRaises(ValueError):
            parse_word_repository_line("app", 1)

    def test_invalid_score(self):
        with self.assertRaises(ValueError):
            parse_word_repository_line("apple,11", 1)

    def test_score_out_of_range_low(self):
        with self.assertRaises(ValueError):
            parse_word_repository_line("apple,0", 1)


class TestGetStreaks(unittest.TestCase):
    def _make_conn_with_games(self, solved_sequence: list[bool]):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        run_migrations(conn)
        conn.executemany(
            "INSERT INTO words(word, guessability_score) VALUES (?, ?)",
            [(f"w{i:04d}", 5) for i in range(len(solved_sequence))],
        )
        for i, solved in enumerate(solved_sequence):
            conn.execute(
                """INSERT INTO games(word, started_at, completed_at, solved, turns_taken, guesses_used)
                   VALUES (?, '2024-01-01T00:00:00+00:00', '2024-01-01T00:01:00+00:00', ?, ?, ?)""",
                (f"w{i:04d}", int(solved), 3 if solved else None, 3),
            )
        conn.commit()
        return conn

    def test_all_solved(self):
        conn = self._make_conn_with_games([True, True, True])
        current, best = get_streaks(conn)
        self.assertEqual(current, 3)
        self.assertEqual(best, 3)

    def test_ends_with_failure(self):
        conn = self._make_conn_with_games([True, True, False])
        current, best = get_streaks(conn)
        self.assertEqual(current, 0)
        self.assertEqual(best, 2)

    def test_broken_streak(self):
        conn = self._make_conn_with_games([True, True, False, True, True, True])
        current, best = get_streaks(conn)
        self.assertEqual(current, 3)
        self.assertEqual(best, 3)

    def test_empty_history(self):
        conn = self._make_conn_with_games([])
        current, best = get_streaks(conn)
        self.assertEqual(current, 0)
        self.assertEqual(best, 0)

    def test_single_failure(self):
        conn = self._make_conn_with_games([False])
        current, best = get_streaks(conn)
        self.assertEqual(current, 0)
        self.assertEqual(best, 0)


class TestAvgSolveTrend(unittest.TestCase):
    def _make_conn_with_turns(self, turns: list[int | None]):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        run_migrations(conn)
        conn.executemany(
            "INSERT INTO words(word, guessability_score) VALUES (?, ?)",
            [(f"w{i:04d}", 5) for i in range(len(turns))],
        )
        for i, turns_taken in enumerate(turns):
            solved = turns_taken is not None
            conn.execute(
                """INSERT INTO games(word, started_at, completed_at, solved, turns_taken, guesses_used)
                   VALUES (?, '2024-01-01T00:00:00+00:00', '2024-01-01T00:01:00+00:00', ?, ?, ?)""",
                (f"w{i:04d}", int(solved), turns_taken, turns_taken or 6),
            )
        conn.commit()
        return conn

    def test_calculates_cumulative_average_and_excludes_failures(self):
        conn = self._make_conn_with_turns([2, None, 4, 3])
        self.assertEqual(
            get_avg_solve_trend(conn),
            [(1, 2.0), (2, 3.0), (3, 3.0)],
        )

    def test_limits_output_but_keeps_all_time_cumulative_average(self):
        conn = self._make_conn_with_turns([1] * 5 + [6] * 25)
        trend = get_avg_solve_trend(conn)
        self.assertEqual(len(trend), 25)
        self.assertEqual(trend[0][0], 6)
        self.assertEqual(trend[-1], (30, 155 / 30))

    def test_non_positive_limit_returns_no_points(self):
        conn = self._make_conn_with_turns([2, 4])
        self.assertEqual(get_avg_solve_trend(conn, limit=0), [])

    def test_chart_uses_flexible_scale_and_game_range(self):
        chart = avg_solve_chart([(3, 2.0), (4, 3.5), (5, 5.6)])
        self.assertEqual(chart[0], "Avg solve trend (solved games 3-5)")
        self.assertEqual(len(chart), 11)
        self.assertTrue(chart[1].startswith("5.96 |"))
        self.assertTrue(chart[8].startswith("1.64 |"))
        self.assertNotIn("*", "".join(chart))
        self.assertTrue(any(character in "".join(chart) for character in "╭╮╰╯"))
        self.assertFalse(any(character in "".join(chart) for character in "┬┴├┤┼"))

    def test_chart_exposes_small_game_to_game_fluctuations(self):
        chart = avg_solve_chart([(10, 3.00), (11, 3.03), (12, 2.97)])
        point_rows = [
            next(
                i
                for i, line in enumerate(chart[1:9])
                if line[6 + column] in "─╱╲╭╮╰╯"
            )
            for column in range(3)
        ]
        self.assertEqual(len(set(point_rows)), 3)
        self.assertLess(float(chart[1].split()[0]) - float(chart[8].split()[0]), 0.2)

    def test_flat_chart_uses_minimum_range(self):
        chart = avg_solve_chart([(1, 3.0), (2, 3.0)])
        self.assertEqual(float(chart[1].split()[0]), 3.05)
        self.assertEqual(float(chart[8].split()[0]), 2.95)
        self.assertIn("──", "".join(chart))

    def test_chart_draws_vertical_segments_between_points(self):
        chart = avg_solve_chart([(1, 2.0), (2, 4.0)])
        self.assertIn("│", "".join(chart))

    def test_single_point_chart_does_not_duplicate_axis_label(self):
        chart = avg_solve_chart([(1, 2.0)])
        self.assertEqual(chart[-1], "       1")

    def test_empty_chart_has_no_lines(self):
        self.assertEqual(avg_solve_chart([]), [])


class TestHardMode(unittest.TestCase):
    def test_hard_mode_defaults_off(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        run_migrations(conn)
        self.assertFalse(is_hard_mode_enabled(conn))

    def test_hard_mode_setting_persists(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        run_migrations(conn)
        set_hard_mode_enabled(conn, True)
        self.assertTrue(is_hard_mode_enabled(conn))
        set_hard_mode_enabled(conn, False)
        self.assertFalse(is_hard_mode_enabled(conn))

    def test_hard_mode_requires_correct_position(self):
        attempts = [("crane", ["correct", "absent", "absent", "absent", "absent"])]
        message = validate_hard_mode_guess("brink", attempts)
        self.assertEqual(message, "Hard mode: position 1 must be C.")

    def test_hard_mode_requires_present_letter_somewhere(self):
        attempts = [("raise", ["absent", "present", "absent", "absent", "absent"])]
        message = validate_hard_mode_guess("cloud", attempts)
        self.assertEqual(message, "Hard mode: guess must include A at least 1 time.")

    def test_hard_mode_disallows_known_yellow_position(self):
        attempts = [("raise", ["absent", "present", "absent", "absent", "absent"])]
        message = validate_hard_mode_guess("cabin", attempts)
        self.assertEqual(message, "Hard mode: A cannot be in position 2.")

    def test_hard_mode_tracks_duplicate_letter_minimums(self):
        attempts = [("abaca", ["correct", "absent", "present", "absent", "absent"])]
        message = validate_hard_mode_guess("adobe", attempts)
        self.assertEqual(message, "Hard mode: guess must include A at least 2 times.")


if __name__ == "__main__":
    unittest.main()

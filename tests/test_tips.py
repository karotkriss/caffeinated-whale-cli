"""Tests for the tips utility module."""

import time
from io import StringIO

from rich.console import Console

from caffeinated_whale_cli.utils.tips import TIPS, TipRotator, TipSpinner, get_tip


class TestTipRotator:
    """Test cases for TipRotator class."""

    def test_tip_rotator_initialization(self):
        """Test that TipRotator initializes correctly."""
        rotator = TipRotator()
        assert rotator.tips == TIPS
        assert rotator.interval == 4.0
        assert rotator.current_index == 0
        assert rotator._shuffled_tips is None

    def test_tip_rotator_custom_tips(self):
        """Test TipRotator with custom tips list."""
        custom_tips = ["Tip 1", "Tip 2", "Tip 3"]
        rotator = TipRotator(tips=custom_tips)
        assert rotator.tips == custom_tips
        assert len(rotator.tips) == 3

    def test_tip_rotator_custom_interval(self):
        """Test TipRotator with custom interval."""
        rotator = TipRotator(interval=2.0)
        assert rotator.interval == 2.0

    def test_shuffle_tips(self):
        """Test that shuffle_tips creates a shuffled list."""
        rotator = TipRotator()
        rotator.shuffle_tips()
        assert rotator._shuffled_tips is not None
        assert len(rotator._shuffled_tips) == len(TIPS)
        assert rotator.current_index == 0
        # All tips should still be present
        assert set(rotator._shuffled_tips) == set(TIPS)

    def test_get_next_tip_shuffles_on_first_call(self):
        """Test that get_next_tip auto-shuffles on first call."""
        rotator = TipRotator()
        assert rotator._shuffled_tips is None
        tip = rotator.get_next_tip()
        assert rotator._shuffled_tips is not None
        assert tip in TIPS
        assert rotator.current_index == 1

    def test_get_next_tip_cycles_through_tips(self):
        """Test that get_next_tip cycles through all tips."""
        custom_tips = ["Tip 1", "Tip 2", "Tip 3"]
        rotator = TipRotator(tips=custom_tips)

        # Get all tips in first cycle
        first_cycle = []
        for _ in range(len(custom_tips)):
            first_cycle.append(rotator.get_next_tip())

        # All tips should be present (order may vary due to shuffle)
        assert set(first_cycle) == set(custom_tips)
        assert rotator.current_index == 0  # Reset after full cycle

    def test_get_next_tip_reshuffles_after_cycle(self):
        """Test that tips are reshuffled after completing a cycle."""
        custom_tips = ["Tip 1", "Tip 2"]
        rotator = TipRotator(tips=custom_tips)

        # Complete one full cycle
        first_shuffle_order = []
        for _ in range(len(custom_tips)):
            first_shuffle_order.append(rotator.get_next_tip())

        # Record the second shuffle order
        second_shuffle_order = []
        for _ in range(len(custom_tips)):
            second_shuffle_order.append(rotator.get_next_tip())

        # Both cycles should have all tips
        assert set(first_shuffle_order) == set(custom_tips)
        assert set(second_shuffle_order) == set(custom_tips)
        # Note: Order may or may not be different due to random shuffle


class TestTipSpinner:
    """Test cases for TipSpinner context manager."""

    def test_tip_spinner_initialization(self):
        """Test that TipSpinner initializes with correct defaults."""
        console = Console(file=StringIO())
        spinner = TipSpinner("Testing", console=console)

        assert spinner.status_message == "Testing"
        assert spinner.console == console
        assert spinner.spinner == "dots"
        assert spinner.enabled is True
        assert isinstance(spinner.rotator, TipRotator)

    def test_tip_spinner_custom_params(self):
        """Test TipSpinner with custom parameters."""
        console = Console(file=StringIO())
        spinner = TipSpinner(
            "Custom test", console=console, spinner="arc", tip_interval=2.0, enabled=False
        )

        assert spinner.status_message == "Custom test"
        assert spinner.spinner == "arc"
        assert spinner.enabled is False
        assert spinner.rotator.interval == 2.0

    def test_tip_spinner_context_manager(self):
        """Test that TipSpinner works as context manager."""
        console = Console(file=StringIO())

        with TipSpinner("Testing", console=console, enabled=False) as spinner:
            assert spinner is not None
            # Context manager should not raise errors
            time.sleep(0.1)

    def test_tip_spinner_update(self):
        """Test that TipSpinner update method works."""
        console = Console(file=StringIO())

        with TipSpinner("Initial message", console=console, enabled=False) as spinner:
            assert spinner.status_message == "Initial message"
            spinner.update("Updated message")
            assert spinner.status_message == "Updated message"

    def test_tip_spinner_format_status_with_tip(self):
        """Test status message formatting with tip."""
        console = Console(file=StringIO())
        spinner = TipSpinner("Test", console=console, enabled=True)

        formatted = spinner._format_status("💡 Sample tip")
        assert "Test" in formatted
        assert "💡 Sample tip" in formatted

    def test_tip_spinner_format_status_without_tip_when_disabled(self):
        """Test status message formatting when tips are disabled."""
        console = Console(file=StringIO())
        spinner = TipSpinner("Test", console=console, enabled=False)

        formatted = spinner._format_status("💡 Sample tip")
        assert "Test" in formatted
        assert "💡 Sample tip" not in formatted

    def test_tip_spinner_short_operation(self):
        """Test TipSpinner with a very short operation."""
        console = Console(file=StringIO())

        with TipSpinner("Quick test", console=console, enabled=True, tip_interval=10.0):
            # Operation shorter than tip_interval
            time.sleep(0.05)

        # Should complete without errors

    def test_tip_spinner_reuse(self):
        """Test that TipSpinner can be reused multiple times."""
        console = Console(file=StringIO())
        spinner = TipSpinner("Test", console=console, enabled=True, tip_interval=0.5)

        # First use
        with spinner:
            time.sleep(0.1)

        # Second use - should work without issues
        with spinner:
            time.sleep(0.1)

        # Third use - verify stop event is properly reset
        with spinner:
            time.sleep(0.1)

        # All uses should complete without errors


class TestGetTip:
    """Test cases for get_tip utility function."""

    def test_get_tip_returns_string(self):
        """Test that get_tip returns a string."""
        tip = get_tip()
        assert isinstance(tip, str)
        assert len(tip) > 0

    def test_get_tip_returns_from_tips_list(self):
        """Test that get_tip returns a tip from TIPS list."""
        tip = get_tip()
        assert tip in TIPS

    def test_get_tip_randomness(self):
        """Test that get_tip can return different tips."""
        # Get multiple tips and check for variety
        tips_received = {get_tip() for _ in range(20)}
        # With 20 calls and 40+ tips, we should get more than one unique tip
        # (unless extremely unlucky with randomness)
        assert len(tips_received) > 1


class TestTipsContent:
    """Test cases for the TIPS content itself."""

    def test_tips_list_exists(self):
        """Test that TIPS list is defined and not empty."""
        assert TIPS is not None
        assert len(TIPS) > 0

    def test_all_tips_are_strings(self):
        """Test that all tips are strings."""
        for tip in TIPS:
            assert isinstance(tip, str)

    def test_all_tips_start_with_emoji(self):
        """Test that all tips start with the lightbulb emoji."""
        for tip in TIPS:
            assert tip.startswith("💡")

    def test_no_duplicate_tips(self):
        """Test that there are no duplicate tips."""
        assert len(TIPS) == len(set(TIPS))

    def test_tips_reasonable_length(self):
        """Test that tips are reasonably sized (not too long)."""
        for tip in TIPS:
            # Tips should be concise - let's say under 120 characters
            assert len(tip) < 120, f"Tip too long: {tip}"

    def test_tips_contain_cwcli_references(self):
        """Test that tips reference cwcli commands and features."""
        # Combine all tips into one string for easier searching
        all_tips_text = " ".join(TIPS)

        # Check for key cwcli concepts
        expected_terms = ["cwcli", "VS Code", "tab completion", "cache", "inspect"]

        for term in expected_terms:
            assert term in all_tips_text, f"Expected term '{term}' not found in tips"

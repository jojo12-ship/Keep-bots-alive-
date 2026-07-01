"""
Rainbet strategy engine.

RPS Ladder:
  - Multi-round bet: each consecutive win multiplies the payout
  - Tracks both player and house moves each round
  - Pattern detection on house moves → next move recommendation
  - EV-based cash out recommendation

Limbo:
  - Target multiplier (default ~60x)
  - Kelly criterion bet sizing (fractional Kelly for safety)
  - Tracks session P&L
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

RPS_MOVE = Literal["rock", "paper", "scissors"]

BEATS: dict[str, str] = {
    "rock":     "paper",     # paper beats rock
    "paper":    "scissors",  # scissors beats paper
    "scissors": "rock",      # rock beats scissors
}
LOSES_TO: dict[str, str] = {v: k for k, v in BEATS.items()}
EMOJI: dict[str, str] = {
    "rock":     "🪨",
    "paper":    "📄",
    "scissors": "✂️",
}


# ── RPS engine ────────────────────────────────────────────────────────────────

@dataclass
class RPSRound:
    player: str
    house: str
    outcome: str   # "win" | "loss" | "tie"
    round_num: int
    multiplier_after: float


@dataclass
class RPSState:
    # Full round-by-round history
    rounds: list[RPSRound] = field(default_factory=list)

    # Current active bet
    current_round: int = 0          # 0 = no active bet
    current_multiplier: float = 1.0
    multiplier_per_win: float = 1.96  # Rainbet default; user can update

    # Session totals
    bets_won: int = 0     # bets where they cashed out or won final round
    bets_lost: int = 0
    session_start: datetime = field(default_factory=datetime.utcnow)

    @property
    def house_history(self) -> list[str]:
        return [r.house for r in self.rounds]

    @property
    def player_history(self) -> list[str]:
        return [r.player for r in self.rounds]

    @property
    def total_rounds(self) -> int:
        return len(self.rounds)

    @property
    def in_bet(self) -> bool:
        return self.current_round > 0

    def start_bet(self) -> None:
        self.current_round = 1
        self.current_multiplier = 1.0

    def next_multiplier(self) -> float:
        """What the multiplier will be after winning the current round."""
        return round(self.current_multiplier * self.multiplier_per_win
                     if self.current_multiplier > 1.0
                     else self.multiplier_per_win, 4)

    def record_round(self, player: str, house: str) -> str:
        """Record one round. Returns 'win'|'loss'|'tie'."""
        player = player.lower()
        house = house.lower()

        if BEATS[house] == player:   # player is what beats house → win
            outcome = "win"
            self.current_multiplier = self.next_multiplier()
        elif BEATS[player] == house:  # house is what beats player → loss
            outcome = "loss"
        else:
            outcome = "tie"

        self.rounds.append(RPSRound(
            player=player,
            house=house,
            outcome=outcome,
            round_num=self.current_round,
            multiplier_after=self.current_multiplier,
        ))

        if outcome == "loss":
            self.bets_lost += 1
            self.current_round = 0
            self.current_multiplier = 1.0
        elif outcome == "win":
            self.current_round += 1

        return outcome

    def cash_out(self) -> float:
        """Cash out current bet. Returns achieved multiplier."""
        multi = self.current_multiplier
        self.bets_won += 1
        self.current_round = 0
        self.current_multiplier = 1.0
        return multi


# ── Pattern detection ─────────────────────────────────────────────────────────

def _frequency(history: list[str], window: int = 30) -> dict[str, float]:
    recent = history[-window:] if len(history) >= window else history
    if not recent:
        return {"rock": 1/3, "paper": 1/3, "scissors": 1/3}
    c = Counter(recent)
    total = len(recent)
    return {m: c.get(m, 0) / total for m in ("rock", "paper", "scissors")}


def _streak(history: list[str], window: int = 5) -> tuple[str | None, int]:
    if not history:
        return None, 0
    recent = history[-window:]
    if len(set(recent)) == 1:
        return recent[-1], len(recent)
    return None, 0


def _trigram_predict(history: list[str]) -> str | None:
    if len(history) < 4:
        return None
    last = tuple(history[-3:])
    votes: Counter = Counter()
    for i in range(len(history) - 3):
        if tuple(history[i:i+3]) == last and i + 3 < len(history):
            votes[history[i + 3]] += 1
    if not votes:
        return None
    best, count = votes.most_common(1)[0]
    return best if count >= 2 else None


def recommend_move(state: RPSState) -> dict:
    """
    Returns:
      move, confidence, reasons, freq, learning (bool)
    """
    history = state.house_history
    n = len(history)
    freq = _frequency(history, window=30)
    reasons: list[str] = []
    votes: dict[str, float] = {"rock": 0.0, "paper": 0.0, "scissors": 0.0}

    # Need at least 8 rounds before frequency bias is meaningful
    MIN_FREQ_ROUNDS = 8
    # Need at least 12 rounds before trigram patterns are meaningful
    MIN_PATTERN_ROUNDS = 12

    if n < MIN_FREQ_ROUNDS:
        # Not enough data — spread votes evenly, flag as learning
        return {
            "move":       "rock",   # placeholder, shown as "your choice"
            "confidence": 0.0,
            "reasons":    [],
            "freq":       freq,
            "learning":   True,
            "rounds_needed": MIN_FREQ_ROUNDS - n,
        }

    # 1. Frequency bias (only meaningful with enough data)
    most_common = max(freq, key=lambda m: freq[m])
    bias = freq[most_common] - 1/3
    # Scale weight by how much data we have (less aggressive early on)
    data_factor = min(1.0, (n - MIN_FREQ_ROUNDS) / 20)
    if bias > 0.08:
        counter = BEATS[most_common]
        votes[counter] += bias * 2 * data_factor
        reasons.append(
            f"House plays {most_common} {freq[most_common]:.0%} recently → counter with {counter}"
        )

    # 2. Streak (always apply if present)
    streak_move, streak_len = _streak(history, window=4)
    if streak_move and streak_len >= 3:
        counter = BEATS[streak_move]
        votes[counter] += 0.4
        reasons.append(f"{streak_len}-in-a-row {streak_move} streak → play {counter}")

    # 3. Trigram pattern (needs more data)
    if n >= MIN_PATTERN_ROUNDS:
        predicted = _trigram_predict(history)
        if predicted:
            counter = BEATS[predicted]
            votes[counter] += 0.5
            reasons.append(f"Pattern predicts house plays {predicted} → counter with {counter}")

    # Pick best
    if not any(v > 0 for v in votes.values()):
        # No signal — pick least-seen house move's counter
        freq_sorted = sorted(freq.items(), key=lambda x: x[1])
        least_common_house = freq_sorted[0][0]
        best_move = BEATS[least_common_house]
        confidence = 0.38
        reasons.append(f"No strong pattern yet — countering least-seen move ({least_common_house})")
    else:
        best_move = max(votes, key=lambda m: votes[m])
        total = sum(votes.values())
        confidence = min(0.72, votes[best_move] / total) if total > 0 else 0.38

    return {
        "move":       best_move,
        "confidence": confidence,
        "reasons":    reasons,
        "freq":       freq,
        "learning":   False,
        "rounds_needed": 0,
    }


def cashout_advice(state: RPSState) -> dict:
    """
    EV-based cash out advice.
    win_prob ≈ 1/3 (assuming ties are losses; adjust if ties replay).
    EV of continuing = win_prob × next_multi
    Compare to: cash out = current_multi
    """
    win_prob = 1 / 3
    current = state.current_multiplier
    next_m = state.next_multiplier()
    ev_continue = win_prob * next_m
    ev_cashout = current

    if current < 2.0:
        # Not worth cashing out yet — multiplier too low
        recommendation = "continue"
        reason = f"Multiplier too low to cash out ({current:.2f}x) — the risk is worth it"
    elif ev_continue >= ev_cashout * 0.95:
        recommendation = "continue"
        reason = f"EV of continuing (${ev_continue:.2f}x) nearly matches cashing out ({current:.2f}x)"
    else:
        recommendation = "cashout"
        deficit = ((ev_cashout - ev_continue) / ev_cashout) * 100
        reason = (
            f"EV of continuing ({ev_continue:.2f}x) is {deficit:.0f}% below cash out ({current:.2f}x)"
        )

    # Override: always recommend cash out at high multipliers (risk management)
    if current >= 7.0:
        recommendation = "cashout"
        reason = f"🔥 {current:.2f}x is serious money — house edge eats you alive at this depth"

    return {
        "recommendation": recommendation,
        "current_multi":  current,
        "next_multi":     next_m,
        "ev_continue":    ev_continue,
        "reason":         reason,
    }


# ── Limbo engine ──────────────────────────────────────────────────────────────

@dataclass
class LimboState:
    bankroll: float
    target_multiplier: float = 60.0
    kelly_fraction: float = 0.25
    wins: int = 0
    losses: int = 0
    session_pnl: float = 0.0
    session_start: datetime = field(default_factory=datetime.utcnow)

    @property
    def win_prob(self) -> float:
        return 0.99 / self.target_multiplier

    @property
    def total(self) -> int:
        return self.wins + self.losses

    def kelly_bet(self) -> float:
        p = self.win_prob
        b = self.target_multiplier - 1
        q = 1 - p
        kelly = (b * p - q) / b
        return round(max(0.01, kelly * self.kelly_fraction * self.bankroll), 2)

    def record_win(self, bet: float) -> float:
        profit = bet * (self.target_multiplier - 1)
        self.bankroll += profit
        self.session_pnl += profit
        self.wins += 1
        return profit

    def record_loss(self, bet: float) -> float:
        self.bankroll = max(0.0, self.bankroll - bet)
        self.session_pnl -= bet
        self.losses += 1
        return -bet

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0

    @property
    def expected_win_rate(self) -> float:
        return self.win_prob

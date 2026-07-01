"""
Rainbet strategy Telegram bot.

RPS Ladder flow per round:
  1. Tap "New Bet" → bot recommends a move
  2. Tap what YOU played
  3. Tap what the HOUSE played
  4. See result + current multiplier + cash out advice
  5. Tap "Cash Out" or "Next Round"

Limbo: Kelly-sized bets, track W/L and bankroll.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from strategy import (
    BEATS,
    EMOJI,
    LimboState,
    RPSState,
    cashout_advice,
    recommend_move,
)

logger = logging.getLogger("rainbet_bot")

# ── Per-chat state ─────────────────────────────────────────────────────────────

_rps:     dict[int, RPSState]   = {}
_limbo:   dict[int, LimboState] = {}
_mode:    dict[int, str]        = {}   # "rps" | "limbo"
_awaiting: dict[int, str]       = {}   # flow step
_pending_player_move: dict[int, str] = {}   # player's chosen move before house is known


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨📄✂️  RPS Ladder", callback_data="mode:rps"),
            InlineKeyboardButton("🎯  Limbo",          callback_data="mode:limbo"),
        ],
        [
            InlineKeyboardButton("📊  Stats",  callback_data="stats"),
            InlineKeyboardButton("🔄  Reset",  callback_data="reset"),
        ],
    ])


def _rps_idle_menu() -> InlineKeyboardMarkup:
    """No active bet."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲  New Bet",     callback_data="rps:new_bet")],
        [InlineKeyboardButton("📊  Stats",       callback_data="stats"),
         InlineKeyboardButton("⚙️  Set Multi",   callback_data="rps:set_multi")],
        [InlineKeyboardButton("⬅️  Main Menu",   callback_data="menu")],
    ])


def _pick_player_move_menu() -> InlineKeyboardMarkup:
    """Step 1 — what did YOU play?"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨 I played Rock",     callback_data="player:rock"),
            InlineKeyboardButton("📄 I played Paper",    callback_data="player:paper"),
            InlineKeyboardButton("✂️ I played Scissors", callback_data="player:scissors"),
        ],
    ])


def _pick_house_move_menu() -> InlineKeyboardMarkup:
    """Step 2 — what did the HOUSE play?"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨 House: Rock",     callback_data="house:rock"),
            InlineKeyboardButton("📄 House: Paper",    callback_data="house:paper"),
            InlineKeyboardButton("✂️ House: Scissors", callback_data="house:scissors"),
        ],
    ])


def _after_win_menu() -> InlineKeyboardMarkup:
    """After a win — cash out or continue."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Cash Out",     callback_data="rps:cashout"),
            InlineKeyboardButton("➡️ Next Round",   callback_data="rps:next_round"),
        ],
        [InlineKeyboardButton("📊 Stats", callback_data="stats")],
    ])


def _after_loss_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 New Bet",     callback_data="rps:new_bet")],
        [InlineKeyboardButton("📊 Stats",       callback_data="stats"),
         InlineKeyboardButton("⬅️ Main Menu",   callback_data="menu")],
    ])


def _limbo_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Won",    callback_data="limbo:win"),
            InlineKeyboardButton("❌ Lost",   callback_data="limbo:loss"),
        ],
        [
            InlineKeyboardButton("💡 Bet Size",       callback_data="limbo:suggest"),
            InlineKeyboardButton("🎯 Change Target",  callback_data="limbo:change_target"),
        ],
        [
            InlineKeyboardButton("💰 Set Bankroll",   callback_data="limbo:set_bankroll"),
            InlineKeyboardButton("📊 Stats",          callback_data="stats"),
        ],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
    ])


# ── State helpers ──────────────────────────────────────────────────────────────

def _get_rps(chat_id: int) -> RPSState:
    if chat_id not in _rps:
        _rps[chat_id] = RPSState()
    return _rps[chat_id]


def _get_limbo(chat_id: int) -> Optional[LimboState]:
    return _limbo.get(chat_id)


# ── Formatting ─────────────────────────────────────────────────────────────────

def _rps_rec_text(state: RPSState, opening: str = "") -> str:
    rec = recommend_move(state)
    freq = rec["freq"]
    freq_str = "  ".join(f"{EMOJI[m]} {freq[m]:.0%}" for m in ("rock", "paper", "scissors"))

    lines = []
    if opening:
        lines += [opening, ""]

    if rec.get("learning"):
        needed = rec["rounds_needed"]
        lines += [
            f"📊 *Still learning* — need {needed} more round{'s' if needed != 1 else ''} before patterns emerge",
            "",
            "_Play whatever feels right for now — your choice_",
            "",
            f"House freq: {freq_str}",
            f"Rounds tracked: {state.total_rounds}",
        ]
    else:
        move = rec["move"]
        conf = rec["confidence"]
        reasons = rec["reasons"]
        lines += [
            f"*Recommended: {EMOJI[move]} {move.upper()}*  ({conf:.0%} confidence)",
            "",
        ]
        if reasons:
            lines.append("*Why:*")
            for r in reasons:
                lines.append(f"• {r}")
        lines += [
            "",
            f"House freq: {freq_str}",
            f"Rounds tracked: {state.total_rounds}",
        ]
    return "\n".join(lines)


def _rps_stats_text(state: RPSState) -> str:
    total_bets = state.bets_won + state.bets_lost
    wins = sum(1 for r in state.rounds if r.outcome == "win")
    losses = sum(1 for r in state.rounds if r.outcome == "loss")
    ties = sum(1 for r in state.rounds if r.outcome == "tie")
    wr = f"{wins/state.total_rounds:.0%}" if state.total_rounds else "—"

    lines = [
        "*📊 RPS Session Stats*",
        "",
        f"Rounds played: {state.total_rounds}  ({wins}W / {losses}L / {ties}T)",
        f"Round win rate: {wr}",
        f"Bets won (cashed out): {state.bets_won}",
        f"Bets lost: {state.bets_lost}",
        f"Multiplier per win: {state.multiplier_per_win}x",
    ]
    if state.in_bet:
        lines += [
            "",
            f"🟢 *Active bet — Round {state.current_round}*",
            f"Current multiplier: {state.current_multiplier:.2f}x",
        ]
    return "\n".join(lines)


def _limbo_text(state: LimboState) -> str:
    bet = state.kelly_bet()
    pnl = state.session_pnl
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    wr = f"{state.win_rate:.1%}" if state.total else "—"

    return "\n".join([
        f"*🎯 Limbo {state.target_multiplier:.0f}x*",
        "",
        f"💰 Bankroll: *${state.bankroll:.2f}*",
        f"📐 Next bet: *${bet:.2f}*  _(¼ Kelly)_",
        f"Win prob: *{state.expected_win_rate:.2%}*  (~1 in {state.target_multiplier:.0f})",
        "",
        f"Session P&L: {pnl_str}",
        f"Rounds: {state.total}  |  Win rate: {wr}",
    ])


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*🎰 Rainbet Strategy Bot*\n\n"
        "*RPS Ladder* — tracks house patterns, recommends moves, tells you when to cash out\n"
        "*Limbo* — Kelly criterion bet sizing\n\n"
        "Pick a mode:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_menu(),
    )


async def cmd_rps(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _mode[chat_id] = "rps"
    state = _get_rps(chat_id)
    await update.message.reply_text(
        _rps_rec_text(state, f"*🪨📄✂️ RPS Ladder Mode*\n\nTap *New Bet* when you start a new bet on Rainbet."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_rps_idle_menu(),
    )


async def cmd_limbo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _mode[chat_id] = "limbo"
    state = _get_limbo(chat_id)
    if state is None:
        _awaiting[chat_id] = "limbo_bankroll_init"
        await update.message.reply_text(
            "*🎯 Limbo Mode*\n\nWhat's your starting bankroll? (type a number, e.g. `100`)",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            _limbo_text(state), parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu()
        )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    mode = _mode.get(chat_id)
    if mode == "rps":
        _rps.pop(chat_id, None)
        _pending_player_move.pop(chat_id, None)
        _awaiting.pop(chat_id, None)
        await update.message.reply_text("✅ RPS session reset.", reply_markup=_rps_idle_menu())
    elif mode == "limbo":
        _limbo.pop(chat_id, None)
        _awaiting[chat_id] = "limbo_bankroll_init"
        await update.message.reply_text("✅ Reset. What's your new starting bankroll?")
    else:
        await update.message.reply_text("Nothing to reset. Pick a mode first.", reply_markup=_main_menu())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Commands*\n"
        "/start — main menu\n"
        "/rps — RPS ladder mode\n"
        "/limbo — Limbo mode\n"
        "/reset — reset current session\n"
        "/help — this message",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Text input handler ─────────────────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    awaiting = _awaiting.get(chat_id)

    if awaiting == "limbo_bankroll_init":
        try:
            bankroll = float(text.replace("$", "").replace(",", ""))
            assert bankroll > 0
        except (ValueError, AssertionError):
            await update.message.reply_text("Enter a valid number, e.g. `100`", parse_mode=ParseMode.MARKDOWN)
            return
        _limbo[chat_id] = LimboState(bankroll=bankroll)
        _awaiting.pop(chat_id, None)
        await update.message.reply_text(
            _limbo_text(_limbo[chat_id]), parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu()
        )

    elif awaiting == "limbo_set_bankroll":
        try:
            bankroll = float(text.replace("$", "").replace(",", ""))
            assert bankroll > 0
        except (ValueError, AssertionError):
            await update.message.reply_text("Enter a valid number.", parse_mode=ParseMode.MARKDOWN)
            return
        _limbo[chat_id].bankroll = bankroll
        _awaiting.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Bankroll set to ${bankroll:.2f}\n\n" + _limbo_text(_limbo[chat_id]),
            parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu(),
        )

    elif awaiting == "limbo_change_target":
        try:
            target = float(text.replace("x", "").strip())
            assert 2 <= target <= 1000
        except (ValueError, AssertionError):
            await update.message.reply_text("Enter a multiplier between 2–1000, e.g. `60`", parse_mode=ParseMode.MARKDOWN)
            return
        _limbo[chat_id].target_multiplier = target
        _awaiting.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Target set to {target:.0f}x\n\n" + _limbo_text(_limbo[chat_id]),
            parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu(),
        )

    elif awaiting == "rps_set_multi":
        try:
            multi = float(text.replace("x", "").strip())
            assert 1.1 <= multi <= 10.0
        except (ValueError, AssertionError):
            await update.message.reply_text(
                "Enter the multiplier per win shown in Rainbet (e.g. `1.96`)", parse_mode=ParseMode.MARKDOWN
            )
            return
        _get_rps(chat_id).multiplier_per_win = multi
        _awaiting.pop(chat_id, None)
        await update.message.reply_text(
            f"✅ Multiplier per win set to {multi}x",
            reply_markup=_rps_idle_menu(),
        )


# ── Callback handler ───────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data

    # ── Navigation ─────────────────────────────────────────────────────────────
    if data == "menu":
        _awaiting.pop(chat_id, None)
        await query.edit_message_text(
            "*🎰 Rainbet Strategy Bot*\n\nPick a mode:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_menu(),
        )
        return

    if data == "reset":
        mode = _mode.get(chat_id)
        if mode == "rps":
            _rps.pop(chat_id, None)
            _pending_player_move.pop(chat_id, None)
            await query.edit_message_text("✅ RPS session reset.", reply_markup=_rps_idle_menu())
        elif mode == "limbo":
            _limbo.pop(chat_id, None)
            _awaiting[chat_id] = "limbo_bankroll_init"
            await query.edit_message_text("✅ Reset. What's your new starting bankroll?")
        else:
            await query.edit_message_text("Pick a mode first.", reply_markup=_main_menu())
        return

    if data == "stats":
        mode = _mode.get(chat_id)
        lines = ["*📊 Session Stats*", ""]
        rps = _rps.get(chat_id)
        if rps:
            lines.append(_rps_stats_text(rps))
        limbo = _limbo.get(chat_id)
        if limbo:
            pnl = limbo.session_pnl
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            lines += [
                "",
                f"*Limbo {limbo.target_multiplier:.0f}x:* {limbo.wins}W / {limbo.losses}L",
                f"Session P&L: {pnl_str}  |  Bankroll: ${limbo.bankroll:.2f}",
            ]
        if not rps and not limbo:
            lines.append("No sessions started yet.")
        kb = _rps_idle_menu() if mode == "rps" else (_limbo_menu() if mode == "limbo" else _main_menu())
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    # ── Mode select ────────────────────────────────────────────────────────────
    if data == "mode:rps":
        _mode[chat_id] = "rps"
        state = _get_rps(chat_id)
        await query.edit_message_text(
            _rps_rec_text(state, "*🪨📄✂️ RPS Ladder*\n\nTap *New Bet* each time you start a new bet on Rainbet."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_rps_idle_menu(),
        )
        return

    if data == "mode:limbo":
        _mode[chat_id] = "limbo"
        state = _get_limbo(chat_id)
        if state is None:
            _awaiting[chat_id] = "limbo_bankroll_init"
            await query.edit_message_text(
                "*🎯 Limbo Mode*\n\nWhat's your starting bankroll?",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                _limbo_text(state), parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu()
            )
        return

    # ── RPS flow ───────────────────────────────────────────────────────────────
    if data.startswith("rps:"):
        action = data.split(":")[1]
        state = _get_rps(chat_id)

        if action == "new_bet":
            state.start_bet()
            rec = recommend_move(state)
            freq = rec["freq"]
            freq_str = "  ".join(f"{EMOJI[m]} {freq[m]:.0%}" for m in ("rock", "paper", "scissors"))

            lines = ["*🎲 Round 1 — New Bet*", ""]
            if rec.get("learning"):
                needed = rec["rounds_needed"]
                lines += [
                    f"📊 *Still learning* — {needed} more round{'s' if needed != 1 else ''} needed for patterns",
                    "_Your choice for now_",
                ]
            else:
                move = rec["move"]
                conf = rec["confidence"]
                lines += [f"*Play: {EMOJI[move]} {move.upper()}*  ({conf:.0%} confidence)"]
                for r in rec["reasons"]:
                    lines.append(f"• {r}")
            lines += ["", f"House freq: {freq_str}", "", "What did *you* play?"]

            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                reply_markup=_pick_player_move_menu(),
            )
            return

        if action == "next_round":
            rec = recommend_move(state)
            advice = cashout_advice(state)

            lines = [
                f"*Round {state.current_round} — {state.current_multiplier:.2f}x so far*",
                "",
            ]
            if rec.get("learning"):
                needed = rec["rounds_needed"]
                lines.append(f"📊 *Still learning* — {needed} more round{'s' if needed != 1 else ''} needed")
                lines.append("_Your choice_")
            else:
                move = rec["move"]
                conf = rec["confidence"]
                lines.append(f"*Play: {EMOJI[move]} {move.upper()}*  ({conf:.0%} confidence)")
            lines += [
                "",
                f"_Cash out: {advice['current_multi']:.2f}x  |  Next win → {advice['next_multi']:.2f}x_",
                "",
                "What did *you* play?",
            ]
            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                reply_markup=_pick_player_move_menu(),
            )
            return

        if action == "cashout":
            achieved = state.cash_out()
            await query.edit_message_text(
                f"💰 *Cashed out at {achieved:.2f}x!*\n\n"
                f"Bets won: {state.bets_won}  |  Bets lost: {state.bets_lost}\n\n"
                + _rps_rec_text(state),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_rps_idle_menu(),
            )
            return

        if action == "set_multi":
            _awaiting[chat_id] = "rps_set_multi"
            await query.edit_message_text(
                "Enter the per-win multiplier shown in Rainbet's RPS game (e.g. `1.96`):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        return

    # ── Player move selected (step 1) ──────────────────────────────────────────
    if data.startswith("player:"):
        player_move = data.split(":")[1]
        _pending_player_move[chat_id] = player_move
        await query.edit_message_text(
            f"You played: *{EMOJI[player_move]} {player_move.upper()}*\n\nWhat did the *house* play?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_pick_house_move_menu(),
        )
        return

    # ── House move selected (step 2) ───────────────────────────────────────────
    if data.startswith("house:"):
        house_move = data.split(":")[1]
        player_move = _pending_player_move.pop(chat_id, None)
        if not player_move:
            await query.answer("Tap 'New Bet' first.", show_alert=True)
            return

        state = _get_rps(chat_id)
        outcome = state.record_round(player_move, house_move)

        p_emoji = EMOJI[player_move]
        h_emoji = EMOJI[house_move]

        if outcome == "win":
            advice = cashout_advice(state)
            rec = recommend_move(state)

            if advice["recommendation"] == "cashout":
                advice_line = f"⚠️ *Recommend: CASH OUT* — {advice['reason']}"
            else:
                advice_line = f"➡️ *Recommend: Continue* — {advice['reason']}"

            lines = [
                f"✅ *WIN!* {p_emoji} beats {h_emoji}",
                f"",
                f"Multiplier: *{state.current_multiplier:.2f}x*  (next win → {state.next_multiplier():.2f}x)",
                f"",
                advice_line,
                f"",
                f"Next play: *{EMOJI[rec['move']]} {rec['move'].upper()}*  ({rec['confidence']:.0%})",
            ]
            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                reply_markup=_after_win_menu(),
            )

        elif outcome == "loss":
            lines = [
                f"❌ *LOSS* — {h_emoji} beats {p_emoji}",
                f"",
                f"Bet lost. Bets won: {state.bets_won}  |  Bets lost: {state.bets_lost}",
                f"",
                _rps_rec_text(state),
            ]
            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                reply_markup=_after_loss_menu(),
            )

        else:  # tie
            rec = recommend_move(state)
            lines = [
                f"🤝 *TIE* — {p_emoji} vs {h_emoji}",
                f"",
                f"Round replays. Current multiplier: {state.current_multiplier:.2f}x",
                f"",
                f"Next play: *{EMOJI[rec['move']]} {rec['move'].upper()}*  ({rec['confidence']:.0%})",
                f"",
                "What did *you* play?",
            ]
            await query.edit_message_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                reply_markup=_pick_player_move_menu(),
            )
        return

    # ── Limbo callbacks ────────────────────────────────────────────────────────
    if data.startswith("limbo:"):
        action = data.split(":")[1]
        state = _get_limbo(chat_id)

        if action == "set_bankroll":
            _awaiting[chat_id] = "limbo_set_bankroll"
            await query.edit_message_text("Enter your current bankroll (e.g. `250`):", parse_mode=ParseMode.MARKDOWN)
            return

        if action == "change_target":
            _awaiting[chat_id] = "limbo_change_target"
            await query.edit_message_text("Enter target multiplier (e.g. `60`):", parse_mode=ParseMode.MARKDOWN)
            return

        if action == "suggest":
            if state is None:
                await query.answer("Set up Limbo first.", show_alert=True)
                return
            bet = state.kelly_bet()
            await query.edit_message_text(
                f"*💡 Bet: ${bet:.2f}*\n\n"
                f"¼ Kelly at {state.target_multiplier:.0f}x  |  Bankroll: ${state.bankroll:.2f}",
                parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu(),
            )
            return

        if action in ("win", "loss"):
            if state is None:
                await query.answer("Set up Limbo first.", show_alert=True)
                return
            bet = state.kelly_bet()
            if action == "win":
                profit = state.record_win(bet)
                result_line = f"✅ *Won!*  +${profit:.2f}"
            else:
                loss = abs(state.record_loss(bet))
                result_line = f"❌ *Lost*  -${loss:.2f}"

            pnl = state.session_pnl
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            await query.edit_message_text(
                f"{result_line}\n\n"
                f"Bankroll: *${state.bankroll:.2f}*\n"
                f"Session P&L: {pnl_str}\n"
                f"Next bet: *${state.kelly_bet():.2f}*\n"
                f"Rounds: {state.total}  ({state.wins}W / {state.losses}L)",
                parse_mode=ParseMode.MARKDOWN, reply_markup=_limbo_menu(),
            )
            return


# ── App factory ────────────────────────────────────────────────────────────────

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("rps",    cmd_rps))
    app.add_handler(CommandHandler("limbo",  cmd_limbo))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app

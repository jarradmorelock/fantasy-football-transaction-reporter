"""Publish or preview non-Ironbound Sleeper transactions.

This reporter is deliberately isolated from the original Ironbound Sixteen
reporter. It uses its own configuration and state names and never reads the
original reporter's environment variables or state file. Preview mode fetches
and formats real Sleeper history without posting to Discord or changing state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import requests
except ModuleNotFoundError:  # Allows --check-config before dependencies are installed.
    requests = None  # type: ignore[assignment]


DEFAULT_STATE_FILE = "other_leagues_state.json"
DEFAULT_CONFIG_FILE = "other_leagues.json"
DEFAULT_PLAYER_CACHE_FILE = Path(".cache/sleeper_players.json")
USER_AGENT = "other-leagues-ledger-bot/1.0"
STATE_VERSION = 2


class ConfigurationError(ValueError):
    """Raised when league configuration is missing or invalid."""


@dataclass(frozen=True)
class LeagueConfig:
    key: str
    name: str
    sleeper_league_id: str
    webhook_waivers: str
    webhook_trades: str
    rounds: Optional[Tuple[int, ...]] = None
    backfill: bool = False


@dataclass(frozen=True)
class LeagueResult:
    name: str
    transactions: int
    messages: int


@dataclass(frozen=True)
class PreviewResult:
    name: str
    transactions: int
    player_transactions_file: Path
    trade_receipts_file: Path


class HttpClient:
    """Small HTTP adapter that avoids leaking Discord webhook URLs in errors."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        if requests is None:
            raise RuntimeError(
                "The requests package is required; run: pip install -r requirements.txt"
            )
        self.session = session or requests.Session()

    def get_json(self, url: str) -> Any:
        try:
            response = self.session.get(
                url,
                timeout=30,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Sleeper API request failed ({exc.__class__.__name__})"
            ) from exc

        if not response.ok:
            raise RuntimeError(
                f"Sleeper API returned HTTP {response.status_code}"
            )
        return response.json()

    def post_discord(self, webhook: str, message: str) -> None:
        # Discord's content field has a hard limit of 2,000 characters.
        if len(message) > 1950:
            message = message[:1950] + "…"

        try:
            response = self.session.post(
                webhook,
                json={"content": message},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Discord webhook request failed ({exc.__class__.__name__})"
            ) from exc

        if not response.ok:
            raise RuntimeError(
                f"Discord webhook returned HTTP {response.status_code}"
            )


def _resolve_value(value: Any, field: str, env: Mapping[str, str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty string")

    value = value.strip()
    if not value.startswith("env:"):
        return value

    variable = value[4:].strip()
    if not variable:
        raise ConfigurationError(f"{field} contains an empty env: reference")

    resolved = env.get(variable, "").strip()
    if not resolved:
        raise ConfigurationError(
            f"{field} references missing environment variable {variable}"
        )
    return resolved


def _parse_rounds(raw: Any, league_name: str) -> Optional[Tuple[int, ...]]:
    if raw is None or raw == "auto":
        return None
    if not isinstance(raw, list) or not raw:
        raise ConfigurationError(f"{league_name}: rounds must be a non-empty list")

    rounds: List[int] = []
    for value in raw:
        try:
            round_num = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{league_name}: invalid transaction round {value!r}"
            ) from exc
        if round_num < 0:
            raise ConfigurationError(
                f"{league_name}: transaction rounds cannot be negative"
            )
        if round_num not in rounds:
            rounds.append(round_num)
    return tuple(rounds)


def _parse_league(
    raw: Any,
    index: int,
    env: Mapping[str, str],
) -> LeagueConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"leagues[{index}] must be an object")

    key = str(raw.get("key") or "").strip()
    name = str(raw.get("name") or key).strip()
    if not key:
        raise ConfigurationError(f"leagues[{index}].key is required")
    if not name:
        raise ConfigurationError(f"leagues[{index}].name is required")

    webhooks = raw.get("webhooks") or {}
    if not isinstance(webhooks, dict):
        raise ConfigurationError(f"{name}: webhooks must be an object")

    sleeper_league_id = _resolve_value(
        raw.get("sleeper_league_id"),
        f"{name}.sleeper_league_id",
        env,
    )
    webhook_waivers = _resolve_value(
        webhooks.get("waivers") or raw.get("webhook_waivers"),
        f"{name}.webhooks.waivers",
        env,
    )
    webhook_trades = _resolve_value(
        webhooks.get("trades") or raw.get("webhook_trades"),
        f"{name}.webhooks.trades",
        env,
    )
    backfill = raw.get("backfill", False)
    if not isinstance(backfill, bool):
        raise ConfigurationError(f"{name}: backfill must be true or false")

    return LeagueConfig(
        key=key,
        name=name,
        sleeper_league_id=sleeper_league_id,
        webhook_waivers=webhook_waivers,
        webhook_trades=webhook_trades,
        rounds=_parse_rounds(raw.get("rounds"), name),
        backfill=backfill,
    )


def load_leagues(
    env: Mapping[str, str] = os.environ,
    config_path: Optional[Path] = None,
) -> List[LeagueConfig]:
    """Load the isolated other-leagues configuration."""

    raw_json = env.get("OTHER_LEAGUES_CONFIG_JSON", "").strip()
    if raw_json:
        source = "OTHER_LEAGUES_CONFIG_JSON"
    else:
        selected_path = config_path or Path(
            env.get("OTHER_LEAGUES_CONFIG_FILE", DEFAULT_CONFIG_FILE)
        )
        if selected_path.exists():
            source = str(selected_path)
            raw_json = selected_path.read_text(encoding="utf-8")
        else:
            raise ConfigurationError(
                "No other-leagues configuration found. Set "
                "OTHER_LEAGUES_CONFIG_JSON or OTHER_LEAGUES_CONFIG_FILE."
            )

    try:
        document = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {source}: {exc.msg}") from exc

    if isinstance(document, list):
        raw_leagues = document
    elif isinstance(document, dict):
        raw_leagues = document.get("leagues")
    else:
        raw_leagues = None

    if not isinstance(raw_leagues, list) or not raw_leagues:
        raise ConfigurationError(f"{source} must contain a non-empty leagues list")

    leagues: List[LeagueConfig] = []
    keys = set()
    for index, raw in enumerate(raw_leagues):
        if isinstance(raw, dict) and raw.get("enabled") is False:
            continue
        league = _parse_league(raw, index, env)
        if league.key in keys:
            raise ConfigurationError(f"Duplicate league key: {league.key}")
        keys.add(league.key)
        leagues.append(league)

    if not leagues:
        raise ConfigurationError(f"{source} has no enabled leagues")
    return leagues


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "leagues": {}}

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Could not read state file {path}") from exc

    if not isinstance(state, dict):
        raise RuntimeError(f"State file {path} must contain a JSON object")

    if not isinstance(state.get("leagues"), dict):
        raise RuntimeError(
            f"State file {path} is not an other-leagues state file; refusing to use it"
        )
    state["version"] = STATE_VERSION
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def user_name_map(client: HttpClient, league_id: str) -> Dict[str, str]:
    users = client.get_json(f"https://api.sleeper.app/v1/league/{league_id}/users")
    output: Dict[str, str] = {}
    for user in users:
        user_id = user.get("user_id")
        if not user_id:
            continue
        # Prefer a custom team name; fall back to the Sleeper display name.
        metadata = user.get("metadata") or {}
        name = (
            metadata.get("team_name")
            or user.get("display_name")
            or f"User {user_id}"
        )
        output[str(user_id)] = name
    return output


def roster_name_map(
    client: HttpClient,
    league_id: str,
) -> Tuple[Dict[int, str], Dict[int, int]]:
    rosters = client.get_json(
        f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    )
    users = user_name_map(client, league_id)

    roster_names: Dict[int, str] = {}
    user_to_roster: Dict[int, int] = {}
    for roster in rosters:
        roster_id = roster.get("roster_id")
        owner_id = roster.get("owner_id")
        if roster_id is None:
            continue

        roster_id = int(roster_id)
        name = (
            users.get(str(owner_id), f"Roster {roster_id}")
            if owner_id is not None
            else f"Roster {roster_id}"
        )
        roster_names[roster_id] = name
        if owner_id is not None:
            user_to_roster[int(owner_id)] = roster_id

    return roster_names, user_to_roster


def player_name_map(
    client: HttpClient,
    cache_path: Path,
) -> Dict[str, str]:
    cache_date = datetime.now(timezone.utc).date().isoformat()
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cached = None
        if (
            isinstance(cached, dict)
            and cached.get("cached_on") == cache_date
            and isinstance(cached.get("players"), dict)
        ):
            return {
                str(player_id): str(name)
                for player_id, name in cached["players"].items()
            }

    players = client.get_json("https://api.sleeper.app/v1/players/nfl")
    output: Dict[str, str] = {}
    for player_id, player in players.items():
        name = (player.get("full_name") or "").strip() or player_id
        position = (player.get("position") or "").strip()
        team = (player.get("team") or "").strip()
        if position and team:
            output[player_id] = f"{name} ({position} {team})"
        elif position:
            output[player_id] = f"{name} ({position})"
        else:
            output[player_id] = name

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_name(f"{cache_path.name}.tmp")
    temporary_path.write_text(
        json.dumps(
            {"cached_on": cache_date, "players": output},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary_path, cache_path)
    return output


def automatic_transaction_rounds(client: HttpClient) -> Tuple[int, ...]:
    nfl_state = client.get_json("https://api.sleeper.app/v1/state/nfl")
    try:
        current_round = int(nfl_state.get("leg") or nfl_state.get("week") or 1)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("Sleeper NFL state did not contain a valid week") from exc
    current_round = max(1, current_round)
    return tuple(sorted({0, 1, current_round - 1, current_round, current_round + 1}))


def fetch_transactions(
    client: HttpClient,
    league_id: str,
    round_num: int,
) -> List[Dict[str, Any]]:
    return client.get_json(
        f"https://api.sleeper.app/v1/league/{league_id}/transactions/{round_num}"
    )


def fetch_league_transactions(
    league: LeagueConfig,
    client: HttpClient,
    rounds: Sequence[int],
) -> List[Dict[str, Any]]:
    transactions: List[Dict[str, Any]] = []
    for round_num in rounds:
        transactions.extend(
            fetch_transactions(client, league.sleeper_league_id, round_num)
        )
    return transactions


def resolve_roster_id(
    value: Any,
    roster_names: Dict[int, str],
    user_to_roster: Dict[int, int],
) -> Optional[int]:
    if value is None:
        return None
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return None
    if candidate in roster_names:
        return candidate
    if candidate in user_to_roster:
        return user_to_roster[candidate]
    return None


def format_player(player_id: str, players: Dict[str, str]) -> str:
    return players.get(player_id, player_id)


def is_final_status(transaction: Dict[str, Any]) -> bool:
    status = (transaction.get("status") or "").lower()
    return status in ("complete", "approved", "executed")


def transaction_timestamp(transaction: Dict[str, Any]) -> int:
    return int(transaction.get("status_updated") or transaction.get("created") or 0)


def transaction_key(transaction: Dict[str, Any]) -> str:
    transaction_id = transaction.get("transaction_id")
    if transaction_id:
        return str(transaction_id)

    # Sleeper normally supplies transaction_id. This stable fallback prevents
    # duplicate posts if an unusual payload does not include it.
    payload = json.dumps(transaction, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunk_lines(header: str, lines: Sequence[str]) -> List[str]:
    messages: List[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            if current.strip():
                messages.append(current)
            current = header + line + "\n"
        else:
            current += line + "\n"
    if current.strip() and current.strip() != header.strip():
        messages.append(current)
    return messages


def format_waiver_receipt(
    transaction: Dict[str, Any],
    roster_names: Dict[int, str],
    players: Dict[str, str],
) -> Optional[List[str]]:
    adds = transaction.get("adds") or {}
    drops = transaction.get("drops") or {}
    if not adds and not drops:
        return None

    per_roster: Dict[int, Dict[str, List[str]]] = {}
    for player_id, roster_id in adds.items():
        roster_id = int(roster_id)
        per_roster.setdefault(roster_id, {"adds": [], "drops": []})
        per_roster[roster_id]["adds"].append(format_player(player_id, players))

    for player_id, roster_id in drops.items():
        roster_id = int(roster_id)
        per_roster.setdefault(roster_id, {"adds": [], "drops": []})
        per_roster[roster_id]["drops"].append(format_player(player_id, players))

    lines: List[str] = ["🧾 **Player Transaction**"]
    for roster_id in sorted(per_roster):
        team = roster_names.get(roster_id, f"Roster {roster_id}")
        roster_adds = per_roster[roster_id]["adds"]
        roster_drops = per_roster[roster_id]["drops"]
        lines.append(f"**{team}**")

        if roster_adds:
            lines.append("➕ **Adds:**")
            lines.extend(roster_adds)
        if roster_drops:
            lines.append("➖ **Drops:**")
            lines.extend(roster_drops)
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return lines


def format_trade_receipt(
    transaction: Dict[str, Any],
    roster_names: Dict[int, str],
    players: Dict[str, str],
    user_to_roster: Dict[int, int],
) -> Optional[List[str]]:
    adds = transaction.get("adds") or {}
    draft_picks = transaction.get("draft_picks") or []
    rosters = (
        transaction.get("roster_ids")
        or transaction.get("consenter_roster_ids")
        or []
    )

    def resolve(value: Any) -> Optional[int]:
        return resolve_roster_id(value, roster_names, user_to_roster)

    received: Dict[int, List[str]] = {}
    for player_id, destination in adds.items():
        roster_id = resolve(destination)
        if roster_id is not None:
            received.setdefault(roster_id, []).append(
                format_player(player_id, players)
            )

    for pick in draft_picks:
        season = pick.get("season", "?")
        round_num = pick.get("round", "?")
        destination = resolve(pick.get("owner_id") or pick.get("roster_id"))
        if destination is None:
            continue

        original = resolve(
            pick.get("roster_id")
            or pick.get("previous_owner_id")
            or pick.get("previous_roster_id")
        )
        original_text = (
            f" (from {roster_names.get(original, f'Roster {original}')})"
            if original is not None
            else ""
        )
        received.setdefault(destination, []).append(
            f"{season} Rd {round_num} Pick{original_text}"
        )

    roster_list: List[int] = []
    for value in rosters:
        roster_id = resolve(value)
        if roster_id is not None and roster_id not in roster_list:
            roster_list.append(roster_id)
    if len(roster_list) < 2:
        roster_list = sorted(received)
    if not received or not roster_list:
        return None

    lines: List[str] = ["🤝 **Trade Receipt**"]
    for roster_id in roster_list:
        team = roster_names.get(roster_id, f"Roster {roster_id}")
        assets = received.get(roster_id, [])
        received_text = ", ".join(assets) if assets else "—"
        lines.append(f"**{team} receives:** {received_text}")
    return lines


def render_receipt_lines(
    transactions: Sequence[Dict[str, Any]],
    roster_names: Dict[int, str],
    players: Dict[str, str],
    user_to_roster: Dict[int, int],
) -> Tuple[List[str], List[str]]:
    """Format transactions through the same path used for Discord messages."""

    player_transaction_lines: List[str] = []
    trade_receipt_lines: List[str] = []
    for transaction in transactions:
        transaction_type = (transaction.get("type") or "").lower()
        if transaction_type in ("waiver", "free_agent", "add_drop"):
            block = format_waiver_receipt(transaction, roster_names, players)
            if block:
                if player_transaction_lines:
                    player_transaction_lines.append("")
                player_transaction_lines.extend(block)
        elif transaction_type == "trade":
            block = format_trade_receipt(
                transaction,
                roster_names,
                players,
                user_to_roster,
            )
            if block:
                if trade_receipt_lines:
                    trade_receipt_lines.append("")
                trade_receipt_lines.extend(block)

    return player_transaction_lines, trade_receipt_lines


def _league_state(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    leagues = state.setdefault("leagues", {})
    entry = leagues.setdefault(
        key,
        {"last_seen_ms": 0, "seen_at_last_ms": []},
    )
    if not isinstance(entry, dict):
        raise RuntimeError(f"State for league {key} must be a JSON object")
    return entry


def _new_final_transactions(
    transactions: Sequence[Dict[str, Any]],
    league_state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    try:
        last_seen = int(league_state.get("last_seen_ms", 0))
    except (TypeError, ValueError):
        last_seen = 0
    seen_at_last = {
        str(value) for value in (league_state.get("seen_at_last_ms") or [])
    }

    unique: Dict[str, Dict[str, Any]] = {}
    for transaction in transactions:
        unique[transaction_key(transaction)] = transaction

    output = []
    for key, transaction in unique.items():
        if not is_final_status(transaction):
            continue
        timestamp = transaction_timestamp(transaction)
        if timestamp > last_seen or (
            timestamp == last_seen and key not in seen_at_last
        ):
            output.append(transaction)
    return sorted(output, key=lambda item: (transaction_timestamp(item), transaction_key(item)))


def _all_final_transactions(
    transactions: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    unique = {
        transaction_key(transaction): transaction
        for transaction in transactions
        if is_final_status(transaction)
    }
    return sorted(
        unique.values(),
        key=lambda item: (transaction_timestamp(item), transaction_key(item)),
    )


def _advance_league_state(
    league_state: Dict[str, Any],
    transactions: Sequence[Dict[str, Any]],
) -> None:
    if not transactions:
        return

    old_timestamp = int(league_state.get("last_seen_ms", 0))
    newest_timestamp = max(transaction_timestamp(item) for item in transactions)
    newest_ids = {
        transaction_key(item)
        for item in transactions
        if transaction_timestamp(item) == newest_timestamp
    }
    if newest_timestamp == old_timestamp:
        newest_ids.update(
            str(value) for value in (league_state.get("seen_at_last_ms") or [])
        )

    league_state["last_seen_ms"] = max(old_timestamp, newest_timestamp)
    league_state["seen_at_last_ms"] = sorted(newest_ids)


def process_league(
    league: LeagueConfig,
    state: Dict[str, Any],
    players: Dict[str, str],
    client: HttpClient,
    automatic_rounds: Tuple[int, ...],
) -> LeagueResult:
    roster_names, user_to_roster = roster_name_map(
        client,
        league.sleeper_league_id,
    )

    transactions = fetch_league_transactions(
        league,
        client,
        league.rounds or automatic_rounds,
    )

    existing_state = state.setdefault("leagues", {}).get(league.key)
    first_run = not isinstance(existing_state, dict)
    league_state = _league_state(state, league.key)
    new_transactions = _new_final_transactions(transactions, league_state)
    if first_run and not league.backfill:
        # Safely establish a baseline for a newly added league instead of
        # flooding Discord with every historical transaction in its rounds.
        _advance_league_state(league_state, new_transactions)
        return LeagueResult(league.name, transactions=0, messages=0)
    if not new_transactions:
        return LeagueResult(league.name, transactions=0, messages=0)

    waiver_lines, trade_lines = render_receipt_lines(
        new_transactions,
        roster_names,
        players,
        user_to_roster,
    )

    messages = 0
    for message in chunk_lines("", waiver_lines):
        client.post_discord(league.webhook_waivers, message)
        messages += 1
    for message in chunk_lines("", trade_lines):
        client.post_discord(league.webhook_trades, message)
        messages += 1

    # State moves only after all of this league's Discord posts have succeeded.
    _advance_league_state(league_state, new_transactions)
    return LeagueResult(
        league.name,
        transactions=len(new_transactions),
        messages=messages,
    )


def _safe_file_key(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in ("-", "_") else "_"
        for character in value
    ).strip("_")
    return safe or "league"


def _write_preview_file(path: Path, lines: Sequence[str], empty_text: str) -> None:
    content = "\n".join(lines).rstrip() if lines else empty_text
    path.write_text(content + "\n", encoding="utf-8")


def preview(
    leagues: Sequence[LeagueConfig],
    output_dir: Path,
    preview_rounds: Sequence[int] = (0, 1, 2, 3),
    client: Optional[HttpClient] = None,
    player_cache_path: Path = DEFAULT_PLAYER_CACHE_FILE,
) -> List[PreviewResult]:
    """Write real formatted history to text without Discord or state access."""

    http = client or HttpClient()
    output_dir.mkdir(parents=True, exist_ok=True)
    players = player_name_map(http, player_cache_path)
    results: List[PreviewResult] = []
    failures: List[str] = []

    for league in leagues:
        try:
            roster_names, user_to_roster = roster_name_map(
                http,
                league.sleeper_league_id,
            )
            transactions = _all_final_transactions(
                fetch_league_transactions(league, http, preview_rounds)
            )
            player_lines, trade_lines = render_receipt_lines(
                transactions,
                roster_names,
                players,
                user_to_roster,
            )

            file_key = _safe_file_key(league.key)
            player_path = output_dir / f"{file_key}-player-transactions.txt"
            trade_path = output_dir / f"{file_key}-trade-receipts.txt"
            _write_preview_file(
                player_path,
                player_lines,
                "No completed player transactions found in the preview rounds.",
            )
            _write_preview_file(
                trade_path,
                trade_lines,
                "No completed trades found in the preview rounds.",
            )
        except Exception as exc:  # Preview every league even if one fails.
            failures.append(f"{league.name}: {exc}")
            print(f"ERROR {league.name}: {exc}", file=sys.stderr)
            continue

        result = PreviewResult(
            name=league.name,
            transactions=len(transactions),
            player_transactions_file=player_path,
            trade_receipts_file=trade_path,
        )
        results.append(result)
        print(
            f"{league.name}: previewed {result.transactions} completed "
            "transaction(s) without sending to Discord"
        )

    if failures:
        raise RuntimeError(f"{len(failures)} league preview(s) failed")
    return results


def run(
    leagues: Sequence[LeagueConfig],
    state_path: Path,
    client: Optional[HttpClient] = None,
) -> List[LeagueResult]:
    http = client or HttpClient()
    state = load_state(state_path)

    # Sleeper's player catalog is league-independent and large, so fetch it once.
    player_cache_path = state_path.parent / DEFAULT_PLAYER_CACHE_FILE
    players = player_name_map(http, player_cache_path)
    automatic_rounds = (
        automatic_transaction_rounds(http)
        if any(league.rounds is None for league in leagues)
        else ()
    )
    results: List[LeagueResult] = []
    failures: List[str] = []

    for league in leagues:
        try:
            result = process_league(
                league,
                state,
                players,
                http,
                automatic_rounds,
            )
        except Exception as exc:  # Keep other league feeds running independently.
            failures.append(f"{league.name}: {exc}")
            print(f"ERROR {league.name}: {exc}", file=sys.stderr)
            continue

        results.append(result)
        save_state(state_path, state)
        print(
            f"{result.name}: {result.transactions} new transaction(s), "
            f"{result.messages} Discord message(s)"
        )

    if failures:
        raise RuntimeError(
            f"{len(failures)} league feed(s) failed; successful league state was saved"
        )
    return results


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration without calling Sleeper or Discord",
    )
    mode.add_argument(
        "--preview-dir",
        type=Path,
        help=(
            "write real formatted transaction history here without calling "
            "Discord or changing state"
        ),
    )
    parser.add_argument(
        "--preview-rounds",
        default="0,1,2,3",
        help="comma-separated Sleeper transaction rounds for preview mode",
    )
    return parser.parse_args(argv)


def _parse_preview_rounds(value: str) -> Tuple[int, ...]:
    raw_values = [item.strip() for item in value.split(",") if item.strip()]
    parsed = _parse_rounds(raw_values, "Preview")
    if parsed is None:
        raise ConfigurationError("Preview rounds cannot be automatic")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        leagues = load_leagues()
        if args.check_config:
            for league in leagues:
                rounds = (
                    "automatic current-week window"
                    if league.rounds is None
                    else ", ".join(str(value) for value in league.rounds)
                )
                first_run = "backfill" if league.backfill else "start from now"
                print(
                    f"{league.name} ({league.key}): rounds {rounds}; {first_run}"
                )
            print(f"Configuration valid for {len(leagues)} league(s)")
            return 0

        if args.preview_dir is not None:
            preview(
                leagues,
                args.preview_dir,
                preview_rounds=_parse_preview_rounds(args.preview_rounds),
            )
            print(
                f"Preview files are ready in {args.preview_dir}; "
                "Discord and other_leagues_state.json were not changed"
            )
            return 0

        state_path = Path(
            os.environ.get("OTHER_LEAGUES_STATE_FILE", DEFAULT_STATE_FILE)
        )
        run(leagues, state_path)
        return 0
    except (ConfigurationError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
